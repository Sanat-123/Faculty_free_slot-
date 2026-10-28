"""
branch_resolver.py - which faculty belong to which BRANCH / DEPARTMENT
======================================================================

The timetable PDFs have no "department" field, but every class name carries
its branch:  3CS-DS-A, 5CS-AI-B, 7CSA ... (CS), 3ME-A (Mechanical),
5ECE-B (Electronics) ...  A teacher belongs to the branches of the classes
he or she teaches.  Nothing here knows any branch, teacher or class name:

  1. BRANCH CODES ARE LEARNED from the class names in the uploaded timetable.
     "3CSA", "5CSE", "7CS-AI-A" and "3CS" are all recognised as branch CS
     because the root "CS" appears in the data and the rest is just a
     section letter or a specialisation (AI, DS, IOT ...).
  2. TEACHER -> BRANCH is counted from the periods each teacher teaches.
     A teacher belongs to a branch when it is the one he/she teaches most
     (primary) or at least FACULTY_BRANCH_MIN_SHARE (default 25 %) of the
     periods.  Teachers can therefore belong to more than one branch.
  3. OPTIONAL DEPARTMENT LIST.  If a CSV/Excel file with a teacher column and
     a department/branch column exists in the project's data/ or config/
     folder (any file name), it overrides the inferred branch for the
     teachers it lists.  Environment variable FACULTY_DEPARTMENT_FILE can
     point to one explicitly.
  4. NATURAL LANGUAGE.  "computer science", "mechanical", "CS branch",
     "faculty of ECE" are matched to the learned codes by initials and
     prefixes - no table of department names is needed.  A branch word must
     sit next to a cue word (branch, department, faculty, teachers ...) so
     ordinary words are never mistaken for branches.
"""

from __future__ import annotations

import csv
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MIN_SHARE = float(os.environ.get("FACULTY_BRANCH_MIN_SHARE", "0.25"))

_ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}

_TITLES = {
    "dr", "prof", "professor", "mr", "mrs", "ms", "miss", "sir", "madam",
    "shri", "smt", "er", "ar",
}

_STOP = {"and", "of", "the", "in", "for", "&", "a", "an"}

# words that show the neighbouring word is about a branch / its people
_CUES = {
    "branch", "branches", "department", "departments", "dept", "depts",
    "stream", "streams", "faculty", "faculties", "teacher", "teachers",
    "professor", "professors", "lecturer", "lecturers", "staff", "wing",
    "engineering", "side",
}


@dataclass(frozen=True)
class BranchRef:
    """A branch (and optionally a sub-branch) mentioned in a question."""

    branch: Optional[str]          # e.g. "CS"  (None = sub-branch only)
    spec: Optional[str] = None     # e.g. "DS"

    @property
    def label(self) -> str:
        return f"{self.branch}-{self.spec}" if self.branch and self.spec \
            else (self.branch or self.spec or "")


# ======================================================================
# class-name parsing (learned from the data)
# ======================================================================

def _tokens(class_name: str) -> List[str]:

    text = str(class_name or "").lower().replace(".", "")

    text = re.sub(r"^\s*\d+\s*", "", text)       # leading semester digits

    return [t for t in re.split(r"[^a-z0-9]+", text) if t]


def _root_of(token: str, first_tokens: Counter) -> str:
    """
    The branch code a class token belongs to.

    1st choice: the shortest prefix that is itself a class token seen in the
                data ("CS" for "CSA", "CSE", "CSAI") - what is left over must
                only be a section letter or a short specialisation.
    2nd choice: when the bare code never appears on its own (only "CSA",
                "CSB", "CSC"), a prefix shared by at least three such
                section-like siblings.
    """

    if len(token) < 3:
        return token

    candidates = []

    for k in range(2, len(token)):

        prefix, rest = token[:k], token[k:]

        if len(rest) > 2 or any(ch.isdigit() for ch in rest):
            continue

        candidates.append(prefix)

    for prefix in candidates:
        if prefix in first_tokens:
            return prefix

    # a token with several longer variants in the data (ECE -> ECEA, ECEB,
    # ECEAI ...) is itself the branch code
    children = sum(
        1 for other in first_tokens
        if other != token
        and other.startswith(token)
        and len(other) - len(token) <= 2
    )

    if children >= 2:
        return token

    for prefix in candidates:

        siblings = sum(
            1 for other in first_tokens
            if other != prefix
            and other.startswith(prefix)
            and len(other) - len(prefix) <= 2
        )

        if siblings >= 3:
            return prefix

    return token


# ======================================================================
# optional department file
# ======================================================================

def _norm_name(value: str) -> str:

    words = [
        w for w in re.split(r"[^a-z0-9]+", str(value or "").lower())
        if w and w not in _TITLES
    ]

    return "".join(words)


def _find_columns(header: List[str]):

    low = [re.sub(r"[^a-z]", "", h.lower()) for h in header]

    teacher = next(
        (i for i, h in enumerate(low)
         if h in {"teacher", "faculty", "facultyname", "teachername",
                  "employeename", "staff", "staffname", "name"}),
        None,
    )

    dept = next(
        (i for i, h in enumerate(low)
         if any(k in h for k in ("department", "dept", "branch", "stream"))),
        None,
    )

    return teacher, dept


def _read_rows(path: Path):

    suffix = path.suffix.lower()

    if suffix == ".csv":

        with open(path, newline="", encoding="utf-8-sig") as handle:
            return [row for row in csv.reader(handle)]

    if suffix in (".xlsx", ".xls"):

        import pandas as pd

        frame = pd.read_excel(path, header=None, dtype=str).fillna("")

        return frame.values.tolist()

    return []


def load_department_map(extra_files: Iterable[str | Path] = ()) -> Dict[str, str]:
    """{normalised teacher name: department label} from the first matching
    CSV/Excel file; {} when there is none."""

    candidates: List[Path] = [Path(p) for p in extra_files]

    explicit = os.environ.get("FACULTY_DEPARTMENT_FILE")

    if explicit:
        candidates.insert(0, Path(explicit))

    for folder in (PROJECT_ROOT / "config", PROJECT_ROOT / "data"):

        if folder.is_dir():
            for pattern in ("*.csv", "*.xlsx", "*.xls"):
                candidates.extend(sorted(folder.glob(pattern)))

    for path in candidates:

        try:

            rows = _read_rows(path)

            if len(rows) < 2:
                continue

            teacher_col, dept_col = _find_columns([str(c) for c in rows[0]])

            if teacher_col is None or dept_col is None:
                continue

            mapping = {}

            for row in rows[1:]:

                if len(row) <= max(teacher_col, dept_col):
                    continue

                name = _norm_name(row[teacher_col])
                dept = str(row[dept_col]).strip()

                if name and dept:
                    mapping[name] = dept

            if mapping:
                return mapping

        except Exception:
            continue

    return {}


# ======================================================================
# the index
# ======================================================================

class BranchIndex:

    def __init__(self, model, department_map: Optional[Dict[str, str]] = None):

        self.model = model

        # ---- 1. learn the branch codes from the class names ----
        class_names = sorted({
            e["class_name"] for e in model.events if e.get("class_name")
        })

        toks = {c: _tokens(c) for c in class_names}

        first = Counter(t[0] for t in toks.values() if t)

        later = Counter()

        for t in toks.values():
            for item in set(t[1:]):
                later[item] += 1

        self.class_info: Dict[str, tuple] = {}

        for name, tk in toks.items():

            if not tk or len(tk[0]) < 2 or tk[0] in _ROMAN \
                    or tk[0].isdigit():
                continue

            root = _root_of(tk[0], first)

            spec = ""

            rest = tk[0][len(root):]

            if len(rest) >= 2 and rest.isalpha():
                spec = rest
            else:
                for item in tk[1:]:
                    if (len(item) >= 2 and item.isalpha()
                            and item not in _ROMAN and later[item] >= 2):
                        spec = item
                        break

            self.class_info[name] = (root.upper(), spec.upper())

        # ---- 2. teacher -> branch counts ----
        faculty = set(model.faculty_set)

        counts: Dict[str, Counter] = defaultdict(Counter)
        spec_counts: Dict[str, Counter] = defaultdict(Counter)
        classes_of: Dict[str, Set[str]] = defaultdict(set)

        seen = set()

        for e in model.events:

            teacher, cls = e.get("teacher"), e.get("class_name")

            if not teacher or not cls or teacher not in faculty:
                continue

            key = (teacher, e.get("day"), e.get("slot"), cls)

            if key in seen:
                continue

            seen.add(key)

            info = self.class_info.get(cls)

            if not info:
                continue

            counts[teacher][info[0]] += 1

            if info[1]:
                spec_counts[teacher][info] += 1

            classes_of[teacher].add(cls)

        # ---- 3. optional department file overrides ----
        self.department_file_used = False

        dept_map = department_map if department_map is not None \
            else load_department_map()

        if dept_map:

            by_norm = {_norm_name(t): t for t in faculty}

            for norm, label in dept_map.items():

                teacher = by_norm.get(norm)

                if teacher:
                    counts[teacher] = Counter({label: 1000})
                    spec_counts.pop(teacher, None)
                    self.department_file_used = True

        self.counts = counts
        self.spec_counts = spec_counts
        self.classes_of = classes_of
        self.faculty = faculty

        # ---- 4. membership ----
        self.primary: Dict[str, str] = {}

        self.members_by_branch: Dict[str, Set[str]] = defaultdict(set)
        self.members_by_spec: Dict[tuple, Set[str]] = defaultdict(set)

        for teacher, counter in counts.items():

            total = sum(counter.values())

            if not total:
                continue

            top = max(counter.values())

            self.primary[teacher] = sorted(
                b for b, n in counter.items() if n == top
            )[0]

            for branch, n in counter.items():
                if n == top or n / total >= MIN_SHARE:
                    self.members_by_branch[branch].add(teacher)

        for teacher, counter in spec_counts.items():
            for (branch, spec), n in counter.items():
                self.members_by_spec[(branch, spec)].add(teacher)

        self.branches = sorted(self.members_by_branch)

        self.specs: Dict[str, Set[str]] = defaultdict(set)

        for (branch, spec) in self.members_by_spec:
            self.specs[branch].add(spec)

        self.unassigned = sorted(
            t for t in faculty if t not in self.primary
        )

        # ---- 5. aliases for natural-language matching ----
        self._aliases: Dict[str, BranchRef] = {}

        for branch in self.branches:

            self._aliases[branch.lower()] = BranchRef(branch)

            words = [w for w in re.split(r"[^a-z0-9]+", branch.lower()) if w]

            if len(words) >= 2:
                self._aliases[" ".join(words)] = BranchRef(branch)
                initials = "".join(w[0] for w in words if w not in _STOP)
                self._aliases.setdefault(initials, BranchRef(branch))

        for branch, specs in self.specs.items():
            for spec in specs:
                self._aliases.setdefault(spec.lower(), BranchRef(None, spec))
                self._aliases[f"{branch.lower()} {spec.lower()}"] = \
                    BranchRef(branch, spec)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def has_branches(self) -> bool:
        return bool(self.branches)

    def members(self, refs: Iterable[BranchRef]) -> Set[str]:
        """Teachers belonging to ANY of the referenced branches."""

        result: Set[str] = set()

        for ref in refs:

            if ref.branch and ref.spec:
                result |= self.members_by_spec.get((ref.branch, ref.spec), set())
            elif ref.branch:
                result |= self.members_by_branch.get(ref.branch, set())
            elif ref.spec:
                for (branch, spec), people in self.members_by_spec.items():
                    if spec == ref.spec:
                        result |= people

        return result

    def profile(self, teacher: str):
        """[(branch, periods, share)] sorted by periods, or []."""

        counter = self.counts.get(teacher) or {}

        total = sum(counter.values())

        if not total:
            return []

        return [
            (b, n if n < 1000 else None, n / total)
            for b, n in sorted(counter.items(), key=lambda x: -x[1])
        ]

    # ------------------------------------------------------------------
    # natural-language detection
    # ------------------------------------------------------------------

    def detect(self, text: str) -> List[BranchRef]:
        """Branches mentioned in a question (needs a nearby cue word)."""

        if not self.branches:
            return []

        words = re.findall(r"[a-z0-9]+", str(text or "").lower())

        if not words:
            return []

        cue_at = [i for i, w in enumerate(words) if w in _CUES]

        if not cue_at:
            return []

        single_aliases = [a for a in self._aliases if " " not in a and len(a) >= 2]

        found: List[BranchRef] = []
        taken: Set[int] = set()

        link = {"of", "from", "in", "for", "the", "at", "to"}

        def near_cue(i, j, tight):
            """
            A cue word (branch, faculty, teachers ...) sits next to the
            window.  Loose = within three words; tight = directly adjacent
            ("mechanical faculty", "faculty of CS") so that a subject such
            as "teaches mechanics" is never taken for a branch.
            """

            if not tight:
                return any(
                    (i - 3 <= c <= j + 3) and not (i <= c <= j)
                    for c in cue_at
                )

            for c in cue_at:

                if c == j + 1 or c == i - 1:
                    return True

                if c == i - 2 and words[i - 1] in link:
                    return True

            return False

        for size in (4, 3, 2, 1):

            for i in range(0, len(words) - size + 1):

                j = i + size - 1

                if any(k in taken for k in range(i, j + 1)):
                    continue

                window = words[i:j + 1]

                if all(w in _STOP or w in _CUES for w in window):
                    continue

                ref, loose = self._match_window(window, single_aliases)

                if ref is not None and not near_cue(i, j, tight=not loose):
                    ref = None

                if ref is not None:

                    taken.update(range(i, j + 1))

                    if ref not in found:
                        found.append(ref)

        # "cs" + "ds" next to each other -> the sub-branch CS-DS
        branches = [r for r in found if r.branch and not r.spec]
        specs = [r for r in found if r.spec and not r.branch]

        if branches and specs:

            combined = []

            for b in branches:
                for s in specs:
                    if s.spec in self.specs.get(b.branch, set()):
                        combined.append(BranchRef(b.branch, s.spec))

            if combined:
                return combined

        return found

    def _match_window(self, window, single_aliases):
        """(BranchRef | None, loose) - loose = a long exact code or phrase."""

        raw = " ".join(window)

        if raw in self._aliases:
            return self._aliases[raw], (len(raw) >= 3)

        if len(window) >= 2:

            content = [w for w in window if w not in _STOP]

            for variant in (
                content,
                [w for w in content if w != "engineering"],
            ):

                initials = "".join(w[0] for w in variant)

                if len(initials) >= 2 and initials in self._aliases:
                    return self._aliases[initials], False

                if len(initials) >= 2 and initials + "e" in self._aliases:
                    return self._aliases[initials + "e"], False

            return None, False

        word = window[0]

        if len(word) >= 5 and word not in _CUES:

            for alias in single_aliases:

                if word.startswith(alias) and word != alias:
                    return self._aliases[alias], False

        return None, False

    # ------------------------------------------------------------------
    # answers
    # ------------------------------------------------------------------

    def overview(self) -> str:

        if not self.branches:
            return "I couldn't find any branch information in the timetable."

        rows = []

        for branch in self.branches:

            primary = sum(1 for t, b in self.primary.items() if b == branch)

            total = len(self.members_by_branch[branch])

            extra = sorted(self.specs.get(branch, set()))

            rows.append(
                f"| {branch} | {primary} | {total} | "
                f"{', '.join(extra) if extra else '—'} |"
            )

        lines = [
            f"**Branches found in the timetable: {len(self.branches)}**",
            "",
            "| Branch | Faculty (main branch) | Faculty teaching it | "
            "Specialisations |",
            "| --- | --- | --- | --- |",
            *rows,
        ]

        if self.unassigned:
            lines += [
                "",
                f"*{len(self.unassigned)} faculty have no classes in the "
                f"timetable, so no branch could be worked out.*",
            ]

        lines += [
            "",
            "*Ask e.g. “list CS faculty” or “who is free on Monday slot 3 "
            "from CS branch”.*",
        ]

        return "\n".join(lines)

    def faculty_list(self, refs: List[BranchRef]) -> str:

        people = sorted(self.members(refs), key=str.lower)

        title = " + ".join(r.label for r in refs)

        if not people:
            return f"No faculty found for the {title} branch."

        lines = [f"**{title} faculty · {len(people)} found**", ""]

        for number, teacher in enumerate(people, 1):

            main = self.primary.get(teacher)

            others = sorted(
                b for b in self.counts.get(teacher, {}) if b != main
            )

            note = ""

            if main and refs and refs[0].branch and main != refs[0].branch:
                note = f" — mainly {main}"
            elif others:
                note = f" — also teaches {', '.join(others)}"

            lines.append(f"{number}. {teacher}{note}")

        return "\n".join(lines)

    def teacher_branch(self, teachers: List[str]) -> str:

        lines = []

        for teacher in teachers:

            profile = self.profile(teacher)

            if not profile:
                lines.append(
                    f"**{teacher}** has no classes in the timetable, so I "
                    f"can't tell the branch."
                )
                continue

            main = profile[0][0]

            parts = ", ".join(
                f"{b} ({round(share * 100)}%)" if n is not None else b
                for b, n, share in profile
            )

            lines.append(
                f"**{teacher}** belongs to the **{main}** branch"
                + (f" (teaches: {parts})" if len(profile) > 1 else "")
                + (" — from the department list." if self.department_file_used
                   and profile[0][1] is None else ".")
            )

        return "\n\n".join(lines)

    def unknown_branch_reply(self, phrase: str) -> str:

        return (
            f"I couldn't find a branch matching “{phrase}” in the timetable. "
            f"Branches found: {', '.join(self.branches) or 'none'}."
        )