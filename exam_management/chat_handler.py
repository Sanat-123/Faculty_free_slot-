"""
Chatbot integration.  handle(query) returns the reply, or None when the message is not
about the managed examination (so the existing chatbot flows keep working unchanged).

The handler only (1) understands the message (nlu), (2) calls the service /
reports, (3) formats the answer.  Names, counts, rooms and dates in every reply come
from the database - the handler never makes them up.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import dashboard as dash
from . import nlu
from . import understanding as und
from .nlu import ExamRequest, FacultyResolver
from .reports import Reporter, fmt_date, md_table, num
from .groups import describe, group_map, normalise_spec
from .restrictions import RestrictionError, RestrictionSpec, describe_scope
from .policy import PolicyError
from .policy_chat import PolicyChat
from .service import ExamError, ExamService, MissingInformation, SessionSpec

_WANT_VERBS = re.compile(r"\b(?:need|needs|want|wants|require\w*|plan\w*|organi[sz]\w*|set\s*up|create|make|put|book|fix|conduct\w*|manage|do|give)\b")
_QUERY_LEAD = re.compile(r"^\W*(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(?:show|list|display|view|tell|what|which|who|whom|whose|how|why|when|where|is|are|does|do|did|report|summari[sz]e|compare|explain|count|find|check)\b")
_EXAM_WORDS = re.compile(r"\b(?:exam\w*|invigilat\w*|duty|duties)\b", re.I)
_ALLOC_VERBS = re.compile(r"\b(?:allocat\w*|assign\w*|schedul\w*|prepar\w*|arrang\w*|distribut\w*|generat\w*|"
                          r"plan|roster|organi[sz]e)\b", re.I)
_YES = re.compile(r"^\W*(?:yes|yep|yeah|ok(?:ay)?|sure)?[\s,]*(?:please\s+)?(?:confirm\w*|go\s+ahead|proceed|do\s+it|apply)\W*$", re.I)
_NO = re.compile(r"^\W*(?:no|cancel|never\s*mind|stop|abort)\W*$", re.I)


class ExamManagementHandler:

    def __init__(self, service: ExamService, today_getter: Callable[[], date] = date.today,
                 catalog: Any = None, llm: Any = None):
        self.catalog = catalog
        self.llm = llm
        self.last_dashboard: Optional[Dict[str, Any]] = None
        self._understanding: Optional[und.RequestUnderstanding] = None
        self.svc = service
        self.repo = service.repo
        self.today = today_getter
        self.rep = Reporter(service)
        self.session_id: Optional[int] = None
        self.draft: Optional[ExamRequest] = None
        self.awaiting: List[str] = []
        self.pending_confirm: Optional[Dict[str, Any]] = None
        self.pending_exclusions: List[Tuple[nlu.Directive, List[int]]] = []
        self.pending_admin_policy: Optional[List[str]] = None
        self.pending_dept: List[Dict[str, Any]] = []
        self.last_exports: List[str] = []
        self.policy_chat = PolicyChat(self)
        self._roster_synced = False
        self._carry_from: Optional[int] = None
        self.last_request: Optional[ExamRequest] = None

    # ------------------------------------------------------------------ helpers
    @property
    def settings(self):
        return self.svc.settings

    def _sync(self) -> None:
        if not self._roster_synced:
            self.svc.sync_roster()
            self.svc.sync_history()
            self._roster_synced = True

    def refresh_roster(self) -> None:
        self._roster_synced = False

    def _session(self) -> Optional[Dict[str, Any]]:
        if self.session_id:
            s = self.repo.session(self.session_id)
            if s and s["status"] in ("planning", "approved"):
                return s
        s = self.repo.latest_session()
        if s:
            self.session_id = s["id"]
        return s

    def _alloc(self) -> Optional[Dict[str, Any]]:
        s = self._session()
        return self.repo.active_allocation(s["id"]) if s else None

    def _latest_alloc(self) -> Optional[Dict[str, Any]]:
        s = self._session()
        return self.repo.latest_allocation(s["id"]) if s else None

    def _resolver(self) -> FacultyResolver:
        return FacultyResolver([(f["id"], f["name"]) for f in self.repo.faculty_all()])

    def _year(self) -> Optional[int]:
        s = self._session()
        return int(s["start_date"][:4]) if s else None

    def _shift_info(self):
        s = self._session()
        if not s:
            return [], {}
        sh = self.repo.session_shifts(s["id"])
        labels = [x["label"] for x in sh]
        times = {x["label"]: (int(x["start_time"][:2]) * 60 + int(x["start_time"][3:5]),
                              int(x["end_time"][:2]) * 60 + int(x["end_time"][3:5])) for x in sh}
        return labels, times

    # ------------------------------------------------------------------- entry
    def handle(self, query: str) -> Optional[str]:
        text = " ".join(str(query or "").split())
        if not text:
            return None
        low = text.lower()
        try:
            return self._route(text, low)
        except MissingInformation as e:
            return self._ask_missing(e.fields)
        except (ExamError, RestrictionError, PolicyError) as e:
            return f"I could not do that: {e}"

    def _route(self, text: str, low: str) -> Optional[str]:
        self._sync()
        sess = self._session()

        # --- answer to a confirmation question
        if self.pending_confirm:
            if _YES.match(text):
                return self._run_confirmed()
            if _NO.match(text):
                self.pending_confirm = None
                return "Okay - nothing was changed."
            if self.pending_confirm.get("kind") in ("callback", "import"):
                self.pending_confirm = None          # a different instruction disarms an import / publish / send request

        # --- approval / discard of the current draft
        if sess and re.match(r"^\W*(?:yes[\s,]*)?(?:please\s+)?(?:approve|finali[sz]e|confirm|go\s+ahead|save|accept)\b", low) \
                and not self.awaiting:
            alloc = self._latest_alloc()
            if alloc and alloc["status"] == "draft":
                return self._approve(alloc, partial=bool(re.search(r"\banyway|\bpartial\b|\beven\s+though", low)))
        if sess and re.search(r"\b(?:discard|drop|delete|reject)\b.{0,25}\b(?:draft|allocation|version|proposal)\b", low):
            alloc = self._latest_alloc()
            if alloc and alloc["status"] == "draft":
                self.svc.cancel_draft(alloc["id"])
                return f"Discarded draft version {alloc['version']}. The previously approved allocation (if any) is unchanged."
        if sess and re.search(r"\b(?:mark|complete|close|finish)\b.{0,30}\bexam\w*\b.{0,20}\b(?:complete\w*|done|over|finished)\b", low):
            n = self.svc.complete_session(sess["id"])
            return f"Marked the examination as completed - its {n} approved duties now count as historical duties in future fairness calculations."

        # --- a clear department / priority instruction is never mistaken for an answer to a pending question
        if self.awaiting:
            r = self.policy_chat.handle(text, low, self._resolver().mentions(text)[0], sess)
            if r is not None:
                return r

        # --- follow-up of an allocation request that lacked information
        if self.awaiting:
            filled = self._continue_request(text, low)
            if filled is not None:
                return filled

        # --- regenerate / another allocation
        if sess and re.search(r"\b(?:another|different|alternative|new|fresh|re-?generate|re-?shuffle|re-?do)\b.{0,40}"
                              r"\b(?:allocation|distribution|arrangement|duties|roster|assignment|version)\b", low) \
                and not re.search(r"\bwhy\b", low):
            return self._regenerate(text, low, vary=bool(re.search(r"\b(?:another|different|alternative)\b", low)))
        if sess and re.search(r"\bre-?generate\b|\b(?:generate|allocate|create|prepare|redo|run|do)\b.{0,25}\bagain\b", low):
            return self._regenerate(text, low, vary=False)

        resolver = self._resolver()
        mentions, unmatched = resolver.mentions(text)
        labels, times = self._shift_info()

        # --- remove one person from a date and replace them
        if sess:
            rep = nlu.parse_replace(text, mentions, labels, self.today(), self._year(), self.settings.default_year_window_days)
            if rep and self._latest_alloc():
                return self._replace(rep)

        # --- who belongs to which department / group (CS vs non-CS ...)
        r = self._departments(text, low, mentions, unmatched)
        if r is not None:
            return r

        # --- a NEW allocation request that carries its own dates is never mistaken for an exclusion instruction
        if self._is_allocation_request(text, low) and self._has_own_dates(text):
            return self._new_request(text, low)

        # --- a change to the CURRENT examination ("make it two shifts", "prefer faculty with fewer than 8 duties")
        r = self._modify_request(text, low)
        if r is not None:
            return r

        # --- department exclusion / priority tiers / file import (data-driven, see policy_chat)
        r = self.policy_chat.handle(text, low, mentions, sess)
        if r is not None:
            return r

        # --- administrative responsibilities
        r = self._admin(text, low, mentions, unmatched, sess)
        if r is not None:
            return r

        # --- exclusion / restoration directives
        r = self._directives(text, low, resolver, mentions, unmatched, sess, labels, times)
        if r is not None:
            return r

        # --- new allocation request
        if self._is_allocation_request(text, low):
            return self._new_request(text, low)

        # --- change / clear the group split of the current examination
        if sess:
            r = self._split_command(text, low, sess)
            if r is not None:
                return r

        # --- changing parameters of the current session
        if sess and not self._latest_alloc() is None:
            r = self._change_params(text, low)
            if r is not None:
                return r

        # --- questions about the allocation
        if sess:
            r = self._queries(text, low, mentions, sess)
            if r is not None:
                return r
        return None

    # --------------------------------------------------------------- recognisers
    def _college_roster_loaded(self) -> bool:
        """True once a faculty roster has been IMPORTED: from then on the college-wide engine is authoritative."""
        try:
            return self.repo.conn.execute(
                "SELECT 1 FROM exm_faculty WHERE source LIKE 'import:%' AND active=1 LIMIT 1").fetchone() is not None
        except Exception:
            return False

    def understanding(self) -> und.RequestUnderstanding:
        if self._understanding is None:
            def finder(text: str):
                found, amb = self.policy_chat.find_departments(text)
                return [und.DeptMention(f.label, f.start, f.end) for f in found
                        if not und.is_common_word(text[f.start:f.end])], amb

            self._understanding = und.RequestUnderstanding(self.today, self.settings, finder, self.catalog, self.llm)
        self._understanding.settings = self.settings
        return self._understanding

    def export(self, kind: str, aid: Optional[int] = None) -> Tuple[bytes, str, str]:
        """Official Excel / PDF of the allocation, built from the same data the dashboard shows."""

        import tempfile
        from pathlib import Path
        from . import official_export as oe
        aid = aid or (self._latest_alloc() or {}).get("id")
        if not aid:
            raise ValueError("There is no allocation to export yet.")
        payload = dash.build(self.svc, aid, self.last_request)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("roster." + ("xlsx" if kind == "xlsx" else "pdf"))
            (oe.to_excel if kind == "xlsx" else oe.to_pdf)(payload, path)
            mime = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if kind == "xlsx" else "application/pdf")
            return path.read_bytes(), f"exam_duty_roster_v{payload['version']}.{'xlsx' if kind == 'xlsx' else 'pdf'}", mime

    def consume_dashboard(self) -> Optional[Dict[str, Any]]:
        """The structured result of the last reply (once); the chat UI renders it as cards, tables and tabs."""

        d, self.last_dashboard = self.last_dashboard, None
        return d

    def _parse_req(self, text: str, year: Any = "default") -> ExamRequest:
        """One reader for every message: the rule reader, semesters, department roles, optional language model."""

        yr = self._year() if year == "default" else year
        req = self.understanding().read(text, yr)
        try:
            inc, exc, _ = self.policy_chat.scope_departments(text)
        except Exception:
            inc, exc = [], []
        req.dept_include = list(dict.fromkeys(req.dept_include + inc))
        req.dept_exclude = list(dict.fromkeys(req.dept_exclude + exc))
        return req

    def _has_own_dates(self, text: str) -> bool:
        r = nlu.parse_exam_request(text, self.today(), self._year(), self.settings.default_year_window_days)
        return bool(r.explicit_dates or r.start)

    _SELECT_LEAD = re.compile(r"^\W*(?:please\s+)?(?:find|select|list|get|pick|choose|identify)\b")
    _AVAILABLE_FACULTY = re.compile(r"\b(?:available|eligible|free|suitable)\s+(?:\w+\s+){0,2}(?:faculty|faculties|teachers?|staff|invigilators?|members?)\b")

    def _owns_allocations(self) -> bool:
        return self._college_roster_loaded() or bool(getattr(self.settings, "own_all_allocation_requests", False))

    def _is_allocation_request(self, text: str, low: str) -> bool:
        if not _EXAM_WORDS.search(low):
            return False
        req = nlu.parse_exam_request(text, self.today(), self._year(), self.settings.default_year_window_days)
        details = sum(bool(x) for x in (req.explicit_dates or req.start, req.shifts, req.room_count or req.rooms, req.per_room))
        if _QUERY_LEAD.match(low):
            # "show ..." / "why ..." is a question; "find the available faculty for second-year exams" asks for a roster
            if not (self._owns_allocations() and self._SELECT_LEAD.match(low) and self._AVAILABLE_FACULTY.search(low)):
                return False
        if not _ALLOC_VERBS.search(low) and not (self._SELECT_LEAD.match(low) and self._AVAILABLE_FACULTY.search(low)):
            # "I need exam duty on 20 and 21 Nov, 9 to 11, 12 halls": softer wording counts only when the message
            # also carries real schedule details (at least three of: dates, times, rooms, invigilators per room)
            if details < 3 or not (_WANT_VERBS.search(low) or self._owns_allocations()):
                return False
        if self._owns_allocations():
            # The older planner only knows the timetable's teachers, so every allocation request is handled here
            # - unless it NAMES particular teachers without describing a whole examination (older named flow).
            try:
                named = [m for m in self._resolver().mentions(text)[0] if len(re.sub(r"[^A-Za-z]", "", m.text)) >= 3]
                return not named or details >= 3
            except Exception:
                return True
        multi_day = bool(req.n_days and req.n_days > 1) or bool(req.start and req.end and req.end > req.start) \
            or len(req.explicit_dates) > 1
        # without a roster, single-day requests stay with the existing exam-duty flow
        return multi_day or bool(re.search(r"\b(?:examination|exam)\s+(?:period|session)\b|\bcollege[- ]wide\b", low))

    # ----------------------------------------------------------- new / continued
    def _new_request(self, text: str, low: str) -> str:
        """A NEW examination: nothing from an earlier request is carried over."""

        req = self._parse_req(text)
        self.draft = req
        self._carry_from = None
        self._queue_named_exclusions(text)
        return self._advance(text)

    def _queue_named_exclusions(self, text: str) -> List[str]:
        """People named for exclusion inside the allocation request itself ("... but not Dr. X"); returns their names."""

        resolver = self._resolver()
        mentions, _ = resolver.mentions(text)
        if not mentions:
            return []
        labels, times = self._shift_info()
        queued: List[str] = []
        for d in nlu.parse_directives(text, resolver, mentions, labels, times, self.today(), self._year(),
                                      self.settings.admin_keywords, self.settings.leave_keywords,
                                      self.settings.default_year_window_days):
            ids = [m.resolved for m in d.mentions]
            if not d.needs and all(i is not None for i in ids):
                self.pending_exclusions.append((d, ids))
                queued += [self.repo.faculty(i)["name"] for i in ids]
        return queued

    def _continue_request(self, text: str, low: str) -> Optional[str]:
        assert self.draft is not None
        new = self._parse_req(text, None)
        before = repr(self.draft.to_json())
        # an answer to a contradiction replaces the statement it contradicted
        if new.classes_suspended is True:
            self.draft.avoid_teaching = None
        if new.avoid_teaching:
            self.draft.classes_suspended = None
        self._resolve_ambiguity(low)
        if new.explicit_dates and (self.draft.start or self.draft.end):
            self.draft.start = self.draft.end = self.draft.n_days = None
        self.draft.merge(new)
        queued = self._queue_named_exclusions(text)
        # a bare answer to a single outstanding question
        if len(self.awaiting) == 1:
            key = self.awaiting[0]
            m = re.fullmatch(r"\W*(\d{1,3}|" + "|".join(nlu.NUMWORDS) + r")\W*(?:rooms?|halls?|invigilators?|each)?\W*", low)
            if m and key == "rooms":
                self.draft.room_count = nlu.to_int(m.group(1))
            elif m and key == "per_room":
                self.draft.per_room = nlu.to_int(m.group(1))
            elif key == "suspension":
                if re.match(r"\W*(?:yes|yeah|yep|suspended|they are|true)", low):
                    self.draft.classes_suspended = True
                elif re.match(r"\W*(?:no|nope|not|continue|false)", low):
                    self.draft.classes_suspended = False
        if repr(self.draft.to_json()) == before:
            if queued:
                return ("Noted - " + ", ".join(queued) + " will be excluded from this allocation.\n\n"
                        + self._ask(self.understanding().check(self.draft, text)))
            return None                 # the message told us nothing new -> let other handlers try
        return self._advance(text)

    def _ask_missing(self, fields: List[str]) -> str:
        return "I still need: " + "; ".join(fields) + "."

    def _resolve_ambiguity(self, low: str) -> None:
        amb = self.draft.ambiguous
        roles = amb.get("department_role")
        if roles:
            if re.search(r"\b(?:student|exam\w*|appear\w*|sit\w*|semester)\b", low) and not re.search(r"\b(?:faculty|invigilat\w*|teacher\w*)\b", low):
                self.draft.student_departments = list(dict.fromkeys(self.draft.student_departments + roles))
                amb.pop("department_role")
            elif re.search(r"\b(?:faculty|invigilat\w*|teacher\w*|staff)\b", low):
                self.draft.dept_include = list(dict.fromkeys(self.draft.dept_include + roles))
                amb.pop("department_role")
            elif re.search(r"\bboth\b", low):
                self.draft.student_departments = list(dict.fromkeys(self.draft.student_departments + roles))
                self.draft.dept_include = list(dict.fromkeys(self.draft.dept_include + roles))
                amb.pop("department_role")
        for k in [k for k in amb if k.startswith("llm_")]:
            amb.pop(k)               # the model's reading never overrides the coordinator's own words

    def _advance(self, text: str) -> str:
        req = self.draft
        assert req is not None
        u = self.understanding().check(req, text)
        if u.conflicts or u.gaps:
            self.awaiting = [g.key for g in u.gaps] or ["conflict"]
            return self._ask(u)
        self.awaiting = []
        return self._create_and_generate(req, u.notes, text)

    def _ask(self, u: und.Understanding) -> str:
        """Say what was understood, then ask ONLY for what is truly missing or contradictory."""

        known = self._known_lines(u.req)
        parts = [known] if known else []
        if u.conflicts:
            parts.append("**I need you to settle this:**\n" + "\n".join(f"- {c}" for c in u.conflicts))
        if u.gaps:
            parts.append(("**One more detail is needed:**" if len(u.gaps) == 1 else "**A few details are still needed:**")
                         + "\n" + "\n".join(f"- {g.question}" for g in u.gaps))
        return "\n\n".join(parts)

    def _labels(self, phrases: List[str]) -> Tuple[List[str], List[str]]:
        depts = self.repo.departments()
        res = self.svc.policy.resolver()
        good, bad = [], []
        for ph in phrases:
            if ph in depts:
                good.append(ph)
                continue
            r = res.resolve(ph)
            (good if r.ok else bad).append(r.label if r.ok else ph)
        return list(dict.fromkeys(good)), bad

    def _create_and_generate(self, req: ExamRequest, notes: List[str], text: str, carry_from: Optional[int] = None) -> str:
        dates, dnotes = nlu.expand_dates(req, self.settings.non_working_weekdays)
        notes = list(notes) + dnotes
        if not dates:
            self.awaiting = ["dates"]
            return "I could not work out any examination dates from that. Please give the date(s), or a start date with an end date."
        self._sync()
        opts: Dict[str, Any] = {}
        inc, bad_i = self._labels(req.dept_include)
        exc, bad_e = self._labels(req.dept_exclude)
        if bad_i or bad_e:
            notes.append("Department(s) not found and ignored: " + ", ".join(bad_i + bad_e) +
                         f" (known departments: {', '.join(self.repo.departments()) or 'none'})")
        if inc or exc:
            opts["departments"] = {"include": inc, "exclude": exc}
        if req.group_split:
            spec, gnotes = self._build_split(req.group_split)
            opts["group_split"] = spec
            notes += gnotes
        if req.prohibit_consecutive:
            opts["prohibit_consecutive"] = True
        if req.max_per_day:
            opts["max_per_day"] = req.max_per_day
        if req.duty_rule:
            opts["duty_rule"] = req.duty_rule
        if req.duty_priority:
            opts["duty_priority"] = req.duty_priority
        if req.allow_unverified:
            opts["allow_unverified"] = True
        scope = und.resolve_scope(req, self.catalog or und.NoClasses())
        if req.classes_suspended is False and scope.classes:
            opts["suspended_classes"] = scope.classes
        notes += scope.notes
        if req.duty_rule and (req.duty_rule.get("period") not in (None, "all recorded duties")):
            notes.append(f"Duty counts: you mentioned '{req.duty_rule['period']}', but recorded duties carry no dates, so all recorded "
                         "duties were counted.")
        opts["request"] = req.to_json()
        name = req.name or (f"{req.exam_type.title()} examination" if req.exam_type else "Examination") + f" {dates[0]:%d %b} - {dates[-1]:%d %b %Y}"
        spec = SessionSpec(name=name, dates=dates, shifts=req.shifts, per_room=req.per_room, rooms=req.rooms,
                           room_count=req.room_count, min_duties=req.min_duties, max_duties=req.max_duties,
                           classes_suspended=req.classes_suspended, options=opts)
        sid = self.svc.create_session(spec)
        carry_from = carry_from or self._carry_from
        if carry_from:
            self._carry_rules(carry_from, sid)
        self.session_id = sid
        self.draft = None
        self.last_request = req
        applied = self._apply_pending(sid)
        seed = re.search(r"\bseed\s*(?:=|:|of)?\s*(\d+)", text.lower())
        outcome = self.svc.generate(sid, seed=int(seed.group(1)) if seed else None)
        sess = self.repo.session(sid)
        if sess["options"].get("rooms_generated"):
            tpl = self.settings.room_label_template
            notes.append(f"You gave a room count, so rooms are labelled {tpl.format(n=1)}..{tpl.format(n='N')}. "
                         f"Give real room names in a new request if you want them (list the room names in your request).")
        return self._present(outcome, notes + applied, first=True, req=req)

    def _carry_rules(self, old: int, new: int) -> None:
        """A modified request keeps the exclusions, department rules and priorities already given for it."""

        conn = self.repo.conn
        for table, where in (("exm_restriction", "status='active'"), ("exm_dept_restriction", "status='active'"),
                             ("exm_priority_rule", "1=1")):
            try:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] not in ("id", "session_id")]
                if not cols:
                    continue
                names = ", ".join(cols)
                conn.execute(f"INSERT INTO {table} (session_id, {names}) SELECT ?, {names} FROM {table} WHERE session_id=? AND {where}",
                             (new, old))
                conn.commit()
            except Exception:
                conn.rollback()

    # ---------------------------------------------------------- follow-up changes
    _STRUCTURAL = ("semesters", "years", "student_departments", "dept_include", "dept_exclude", "shifts", "rooms", "room_count",
                   "explicit_dates", "start", "duty_rule", "duty_priority", "avoid_teaching", "exam_type", "class_names",
                   "all_faculty_departments", "college_wide")

    def _modify_request(self, text: str, low: str) -> Optional[str]:
        """
        "Make it two shifts and 2 per room", "give preference to faculty with fewer than 8 duties", "also semester 5":
        the message changes the CURRENT examination.  Everything already stated is kept; only what is new replaces it.
        A message with its own dates and an allocation verb is a NEW examination and never gets here.
        """

        sess = self._session()
        stored = (sess or {}).get("options", {}).get("request") if sess else None
        instruction = re.match(r"^\W*give\s+(?:\w+\s+){0,2}?(?:preference|priority|precedence)\b", low)
        if not sess or not stored or (self._is_question(low) and not instruction) or self.awaiting:
            return None
        # restrictions on people or departments ("exclude CS from 10 Nov", "Dr. X is unavailable on 12 Nov") are not edits
        if re.search(r"\b(?:exclud\w*|unavailab\w*|restor\w*|on\s+leave|leave\s+on|available\s+again|replac\w*|remov\w*)\b", low):
            return None
        try:
            if any(len(re.sub(r"[^A-Za-z]", "", m.text)) >= 3 for m in self._resolver().mentions(text)[0]):
                return None
        except Exception:
            pass
        new = self._parse_req(text, self._year())
        changed = [k for k in self._STRUCTURAL if getattr(new, k) not in (None, [], {})]
        date_cue = re.match(r"^\W*(?:please\s+)?(?:now\s+|also\s+|and\s+)?(?:make|change|update|modify|switch|move|shift|instead|reschedule|extend|postpone)\b", low)
        if not date_cue:
            changed = [k for k in changed if k not in ("explicit_dates", "start")]
        if new.per_room and not re.search(r"\binvigilator", low):
            changed.append("per_room")
        if new.shifts_per_day_hint and not new.shifts:
            changed.append("shifts_hint")
        if not changed:
            return None
        continuing = re.match(r"^\W*(?:please\s+)?(?:now|also|and|then|make|change|update|modify|switch|instead|increase|reduce|"
                              r"add|set|use|give|prefer|keep|let|can\s+you|could\s+you|but|actually|rather)\b", low)
        if self._is_allocation_request(text, low) and not continuing:
            return None                       # a complete new request: nothing from the previous examination is carried over
        if (new.explicit_dates or new.start) and self._is_allocation_request(text, low) and not re.match(r"^\W*(?:make|change|update|modify|switch|instead)\b", low):
            return None
        alloc = self._latest_alloc()
        if alloc and alloc["status"] == "approved":
            return ("This examination is already **approved and saved**, so I will not rewrite it. Ask for a different draft "
                    "with a new request (give its dates), or replace individual duties (*remove <name> from <date>*).")
        merged = ExamRequest.from_json(stored)
        if new.explicit_dates and (merged.start or merged.end):
            merged.start = merged.end = merged.n_days = None
        if (new.start or new.end) and merged.explicit_dates:
            merged.explicit_dates = []
        merged.merge(new)
        if "shifts_hint" in changed:
            merged.shifts = [] if len(merged.shifts) != new.shifts_per_day_hint else merged.shifts
        self.draft = merged
        self._carry_from = sess["id"]
        msg = self._advance(text)
        if self.draft is None and self._carry_from:      # it generated: retire the superseded draft
            self._retire(self._carry_from)
        return msg

    def _retire(self, old: int) -> None:
        try:
            alloc = self.repo.latest_allocation(old)
            if alloc and alloc["status"] == "draft":
                self.svc.cancel_draft(alloc["id"])
            self.repo.update_session(old, status="cancelled")
        except Exception:
            pass
        self._carry_from = None

    def _known_lines(self, req: ExamRequest) -> str:
        """Everything understood so far, in one compact block - so a coordinator sees nothing was dropped."""

        bits = []
        if req.exam_type:
            bits.append(f"**Type:** {req.exam_type}")
        if req.semesters:
            bits.append("**Semester:** " + ", ".join(map(str, req.semesters)))
        if req.years:
            bits.append("**Year:** " + ", ".join(str(y) for y in req.years))
        if req.student_departments:
            bits.append("**Students from:** " + ", ".join(req.student_departments))
        if req.dept_include:
            bits.append("**Faculty from:** " + ", ".join(req.dept_include))
        elif req.all_faculty_departments:
            bits.append("**Faculty from:** all departments")
        if req.dept_exclude:
            bits.append("**Not from:** " + ", ".join(req.dept_exclude))
        if req.explicit_dates:
            ds = sorted(req.explicit_dates)
            bits.append("**Dates:** " + ", ".join(f"{d:%d %b %Y}" for d in ds))
        elif req.start and req.end:
            bits.append(f"**Dates:** {req.start:%d %b} to {req.end:%d %b %Y}")
        elif req.start and req.n_days:
            bits.append(f"**Dates:** {req.n_days} days from {req.start:%d %b %Y}")
        if req.shifts:
            bits.append("**Shifts:** " + ", ".join(f"{s['label']} {dash.clock12(s['start'])}-{dash.clock12(s['end'])}" for s in req.shifts))
        elif req.shifts_per_day_hint:
            bits.append(f"**Shifts per day:** {req.shifts_per_day_hint}")
        if req.room_count or req.rooms:
            bits.append(f"**Rooms:** {len(req.rooms) if req.rooms else req.room_count}")
        if req.per_room:
            bits.append(f"**Invigilators per room:** {req.per_room}")
        if req.duty_rule:
            from .duty_rules import DutyRule
            bits.append("**Duty count:** " + DutyRule.from_dict(req.duty_rule).describe())
        if req.duty_priority:
            bits.append("**Preference:** lowest duty counts first")
        if req.min_duties is not None or req.max_duties is not None:
            bits.append(f"**Duties per faculty here:** min {req.min_duties if req.min_duties is not None else '-'}, "
                        f"max {req.max_duties if req.max_duties is not None else '-'}")
        if req.max_per_day:
            bits.append(f"**Per day:** at most {req.max_per_day} duty")
        if req.prohibit_consecutive:
            bits.append("**Consecutive days:** not allowed")
        if req.avoid_teaching:
            bits.append("**Teaching clashes:** avoided")
        if req.simultaneous_rooms_forbidden:
            bits.append("**Two rooms at once:** never")
        if req.unmapped:
            bits.append("**Also noted (cannot be applied automatically):** " + " / ".join(req.unmapped))
        return ("**What I understood** - " + "  ·  ".join(bits)) if bits else ""

    def _apply_pending(self, sid: int) -> List[str]:
        notes = []
        specs: List[RestrictionSpec] = []
        for d, ids in self.pending_exclusions:
            specs += self._specs(d, ids, sid)
        if specs:
            res = self.svc.restrictions.add_bulk(specs)
            notes.append(f"Applied {len(res.created)} exclusion(s) you gave earlier.")
            for spec, err in res.errors:
                notes.append(f"Could not apply an earlier exclusion for {self.repo.faculty(spec.faculty_id)['name']}: {err}")
        self.pending_exclusions = []
        for op in self.pending_dept:
            try:
                self.svc.policy.exclude_department(op["department"], kind=op["kind"], session_id=sid,
                                                   start=op["start"], end=op["end"], shift_label=op["shift"],
                                                   reason=op["reason"])
                notes.append(f"Applied your earlier exclusion of {op['department']}.")
            except PolicyError as e:
                notes.append(f"Could not apply an earlier department exclusion: {e}")
        self.pending_dept = []
        if self.pending_admin_policy is not None:
            out = self.svc.restrictions.apply_admin_policy(sid, self.pending_admin_policy or None)
            notes.append(f"Administrative exclusion applied to {len(out['created'])} faculty member(s)."
                         if not out["none_found"] else
                         "No administrative responsibilities are recorded, so nobody was excluded on that basis.")
            self.pending_admin_policy = None
        return notes

    # ---------------------------------------------------------------- presenting
    def _present(self, outcome, notes: List[str], first: bool = False, req: Optional[ExamRequest] = None) -> str:
        aid = outcome.allocation_id
        if outcome.status == "unchanged":
            return "\n".join(outcome.notes)
        try:
            self.last_dashboard = dash.build(self.svc, aid, req or self.last_request, [n for n in notes if n])
            self.last_dashboard["headline"] = self._headline(self.last_dashboard)
        except Exception as error:             # the chat text below is still complete without it
            print(f"dashboard unavailable: {error}")
            self.last_dashboard = None
        parts = []
        if outcome.status == "infeasible":
            parts.append("**No valid allocation exists under the current constraints.**")
        parts.append(self.rep.render_headline(aid))
        parts.append("**At a glance**\n" + self.rep._summary_table(aid))
        a = self.repo.allocation(aid)
        f = (a["quality"] or {}).get("fairness") or {}
        if f:
            parts.append(f"⚖️ **Fairness** - new duties per eligible faculty: avg {f['avg_new']} (min {f['min_new']}, max {f['max_new']}) · "
                         f"{f['zero_duty_eligible']} eligible with no duty · {f['consecutive_pairs']} consecutive-day pair(s) · "
                         f"combined-workload spread (std) {f['workload_std']}")
        try:
            note = self.rep.history_note(aid)
        except Exception:
            note = ""
        if note:
            parts.append(note)
        if self.repo.unfilled(aid):
            parts.append("**Unfilled seats**\n" + self.rep.render_unfilled(aid) + "\n\n" + self._suggestions(aid))
        if self.repo.duties(aid):
            parts.append("---\n## 🗓️ Day-wise duty chart\n" + self.rep.render_schedule(aid, max_rows=self.settings.chat_schedule_max_rows))
        below = (a["quality"] or {}).get("below_minimum")
        if below:
            parts.append(f"Minimum-duty shortfall for {len(below)} faculty (eligibility/maximum limits prevent more without artificial duties).")
        parts += [n for n in notes if n]
        if a["status"] == "draft":
            first_day = date.fromisoformat(self.repo.session(a["session_id"])["start_date"])     # examples use YOUR dates
            day = f"{first_day.day} {first_day:%B}"
            parts.append("---\nThis is a **draft awaiting approval**. You can say: *approve*, *generate another allocation*, "
                         f"*generate the PDF*, *exclude <a faculty name> on {day}*, *show duties on {day}*, "
                         "*show department-wise distribution*, or *why did <name> receive more duties than <name>*.")
        return "\n\n".join(parts)

    @staticmethod
    def _headline(d: Dict[str, Any]) -> str:
        cards = {c["label"]: c["value"] for c in d["cards"]}
        tail = ("every position is filled" if d["complete"] else f"{cards['Positions unfilled']} position(s) could not be filled")
        return (f"**{d['status_text']}** - {cards['Positions assigned']} of {cards['Positions required']} positions assigned "
                f"across {cards['Total shifts']} · {tail}. Validation: **{d['validation_overall']}**.")

    def _suggestions(self, aid: int) -> str:
        un = self.repo.unfilled(aid)
        need = len(un)
        a = self.repo.allocation(aid)
        s = self.repo.session(a["session_id"])
        tips = ["Reduce invigilators per room or the number of rooms.",
                "Restore some excluded/unavailable faculty (say 'show exclusions' to review them)."]
        if s["max_duties"] is not None:
            tips.insert(0, f"Raise the maximum duties per faculty (now {s['max_duties']}).")
        if s["options"].get("prohibit_consecutive"):
            tips.append("Allow consecutive-day duties.")
        return f"{need} seat(s) are unfilled. Possible adjustments:\n" + "\n".join(f"- {t}" for t in tips)

    # --------------------------------------------------------------- regenerate
    def _regenerate(self, text: str, low: str, vary: bool) -> str:
        sess = self._session()
        seed = re.search(r"\bseed\s*(?:=|:|of)?\s*(\d+)", low)
        outcome = self.svc.generate(sess["id"], seed=int(seed.group(1)) if seed else None, avoid_previous=vary)
        return self._present(outcome, [])

    def _approve(self, alloc: Dict[str, Any], partial: bool) -> str:
        res = self.svc.approve(alloc["id"], allow_partial=partial)
        if not res["approved"]:
            return "I cannot approve this version yet:\n" + "\n".join(f"- {p}" for p in res["problems"])
        parts = [f"Approved allocation version {alloc['version']}. It is now the live allocation and any earlier approved "
                 f"version is marked superseded (history is kept)."]
        parts.append(self.rep.render_summary(alloc["id"]))
        return "\n\n".join(parts)

    # -------------------------------------------------------------- replacement
    def _replace(self, rep: nlu.ReplaceRequest) -> str:
        alloc = self._latest_alloc()
        fid = rep.mention.resolved
        if fid is None:
            return self._ambiguous([rep.mention])
        out = self.svc.replace_duty(alloc["id"], fid, rep.on, shift_label=rep.shift, reason=rep.reason,
                                    allow_wider=rep.allow_wider)
        who = self.repo.faculty(fid)["name"]
        parts = [f"Removed **{who}** from {fmt_date(rep.on)}"
                 + (f" ({rep.shift} shift)" if rep.shift else "") + (f" - {rep.reason}" if rep.reason else "")
                 + ". The restriction is recorded so later allocations respect it.",
                 "**Changes**\n" + self.rep.render_changes(out.changes)]
        parts += out.notes
        others = [ch for ch in out.changes if ch["old"] and ch["old"] != who]
        if others:
            parts.append("Some other duties also changed because earlier restrictions had made them invalid: "
                         + ", ".join(sorted({ch["old"] for ch in others})) + ".")
        new = self.repo.allocation(out.allocation_id)
        errs = self.svc.validate_allocation(out.allocation_id)
        parts.append("All hard constraints re-validated: " + ("OK." if not errs else "; ".join(errs[:3])))
        if self.repo.unfilled(out.allocation_id):
            parts.append("**Unfilled seat(s) remain**\n" + self.rep.render_unfilled(out.allocation_id))
        parts.append(f"This is draft version {new['version']}; unaffected assignments were kept. The previous approved version stays "
                     f"in force until you say *approve*.")
        return "\n\n".join(parts)

    # ------------------------------------------------------------ groups / departments
    def _build_split(self, parsed: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        depts = self.repo.departments()
        notes: List[str] = []

        def pick(word: str) -> Tuple[str, List[str]]:
            found, _ = nlu.match_departments([word], depts)
            return (found[0] if found else nlu._group_label(word)), (found or [nlu._group_label(word)])

        g1, labels1 = pick(parsed["base"])
        if parsed.get("second"):
            g2, labels2 = pick(parsed["second"])
        else:
            g2, labels2 = "Non-" + g1, ["*"]
        sh = parsed.get("shares") or {}
        shares = {g1: sh.get("first", 1.0), g2: sh.get("second", 1.0)}
        spec = normalise_spec({"groups": {g1: labels1, g2: labels2}, "shares": shares})
        gm = group_map(((f["id"], f["department"] or "") for f in self.repo.faculty_all()), spec)
        for g in spec["groups"]:
            if not any(v == g for v in gm.values()):
                notes.append(f"No faculty are in the '{g}' group yet, so its seats will be filled by the other group. "
                             f"Tell me who belongs to it, e.g. *Dr. X and Dr. Y are {g} faculty*, or import a department file.")
        notes.insert(0, "Duty split: " + describe(spec))
        return spec, notes

    def _split_command(self, text: str, low: str, sess: Dict[str, Any]) -> Optional[str]:
        if re.search(r"\b(?:no|remove|cancel|drop|stop|without|clear)\b.{0,25}\b(?:group\s+split|split|cs\s*/?\s*non)", low) \
                and re.search(r"\bsplit\b", low):
            self.svc.update_session(sess["id"], group_split=None)
            return "Removed the group split. Say *generate allocation again* to apply it."
        if self._is_question(low):
            return None
        parsed = nlu.parse_group_split(text)
        if not parsed:
            return None
        spec, notes = self._build_split(parsed)
        self.svc.update_session(sess["id"], group_split=spec)
        return "\n\n".join(notes + ["Updated the examination. Say *generate allocation again* to apply the split."])

    def _departments(self, text: str, low: str, mentions, unmatched) -> Optional[str]:
        m = re.search(r"\bimport\s+(?:the\s+)?departments?\s+(?:from|file|list)?\s*[:\"']?\s*([^\s\"']+\.(?:csv|txt))", text, re.I)
        if m:
            out = self.svc.import_department_file(m.group(1))
            self.refresh_roster()
            msg = f"Imported department labels for {out['updated']} faculty from {m.group(1)}."
            if out["unknown"]:
                msg += " Not found in the faculty list: " + ", ".join(out["unknown"][:15]) + "."
            return msg
        if re.search(r"\b(?:exclud\w*|unavailab\w*|restor\w*|available|invigilat\w*\s+duty|remove)\b", low):
            return None
        if any(w in low for w in self.settings.admin_keywords):
            return None
        parsed = nlu.parse_dept_assignment(text, self.repo.departments())
        if not parsed or " of " in parsed["label"].lower():
            return None
        ids: List[int] = []
        if mentions:
            if any(mm.resolved is None for mm in mentions):
                return self._ambiguous([mm for mm in mentions if mm.resolved is None])
            ids = list(dict.fromkeys(mm.resolved for mm in mentions))
        elif parsed.get("source"):
            found, _ = nlu.match_departments([parsed["source"]], self.repo.departments())
            if not found:
                return (f"I could not find a department called '{parsed['source']}'. "
                        f"Known departments: {', '.join(self.repo.departments()) or 'none'}.")
            ids = [f["id"] for f in self.repo.faculty_all() if (f["department"] or "") == found[0]]
        if not ids:
            return None
        self.svc.repo.set_department(ids, parsed["label"])
        names = [self.repo.faculty(i)["name"] for i in ids]
        shown = ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "")
        extra = ""
        sess = self._session()
        if sess and sess["options"].get("group_split"):
            extra = " The group split uses these labels - say *generate allocation again* to apply it."
        return f"Recorded {len(ids)} faculty member(s) as **{parsed['label']}**: {shown}.{extra}"

    # ------------------------------------------------------------ administrative
    def _admin(self, text, low, mentions, unmatched, sess) -> Optional[str]:
        if re.search(r"\b(?:show|list|display|who\b.*\bhas|which)\b.{0,40}\badministrat\w*\s+(?:responsibilit\w*|dut\w*|roles?|work)", low):
            rows = self.repo.responsibilities()
            return md_table(["Faculty", "Department", "Responsibility", "From", "Until"],
                            [[r["faculty"], r["department"] or "—", r["role"], r["start_date"] or "—", r["end_date"] or "—"]
                             for r in rows]) if rows else "No administrative responsibilities are recorded yet. Tell me, e.g. 'Dr. X is HOD'."
        if re.search(r"\bexclud\w*\b", low) and re.search(r"\badministrat\w*\s+(?:responsibilit\w*|dut\w*|roles?|work)|"
                                                          r"\b(?:handling|having)\s+administrat\w*", low) and not mentions:
            roles = nlu.parse_roles(text)
            if sess:
                out = self.svc.restrictions.apply_admin_policy(sess["id"], roles or None)
                if out["none_found"]:
                    return ("No matching administrative responsibilities are recorded, so nobody was excluded. Tell me who holds "
                            "which role (e.g. 'Dr. X is HOD', 'Prof. Y is handling NAAC work') and ask again. "
                            "Nothing is assumed from titles.")
                names = ", ".join(sorted({r["faculty_id"] and self.repo.faculty(r["faculty_id"])["name"] for r in out["created"]})) or "(already excluded)"
                return (f"Excluded {len(out['created'])} faculty member(s) with administrative responsibilities"
                        f"{' (roles: ' + ', '.join(roles) + ')' if roles else ''}: {names}.\n\n" + self._after_change())
            self.pending_admin_policy = roles
            return "Noted - I will exclude faculty with recorded administrative responsibilities when the examination is created."
        if mentions and not re.search(r"\b(?:exclud\w*|unavailab\w*|restor\w*|available)\b", low):
            role = nlu.parse_responsibility(text, self.settings.admin_keywords)
            if role:
                ids = [m.resolved for m in mentions if m.resolved]
                if len(ids) != len(mentions):
                    return self._ambiguous([m for m in mentions if m.resolved is None])
                dp = nlu.DateParser(self.today(), self._year())
                hits = dp.find(text)
                start = hits[0].first if hits else None
                end = hits[0].last if hits and hits[0].is_range else None
                for fid in ids:
                    self.repo.add_responsibility(fid, role, text, start, end)
                names = ", ".join(self.repo.faculty(i)["name"] for i in ids)
                return (f"Recorded: {names} - {role}. Say *exclude faculty handling administrative responsibilities* to "
                        f"keep them out of examination duty (nothing is excluded automatically).")
        return None

    # ----------------------------------------------------------------- directives
    def _ambiguous(self, ms: List[nlu.Mention]) -> str:
        lines = []
        for m in ms:
            opts = [f"{self.repo.faculty(c)['name']} ({self.repo.faculty(c)['department'] or '—'})" for c in m.candidates]
            lines.append(f"'{m.text}' could be: " + "; ".join(opts))
        return "I found more than one match, so I changed nothing:\n- " + "\n- ".join(lines) + "\nPlease use the full name."

    def _specs(self, d: nlu.Directive, ids: List[int], sid: Optional[int]) -> List[RestrictionSpec]:
        out = []
        for fid in ids:
            for sc in d.scopes:
                kind = sc.kind
                if kind == "session" and sid is None:
                    continue
                out.append(RestrictionSpec(fid, kind, d.reason or "Excluded by coordinator", d.category,
                                           sid if kind != "permanent" else None, sc.start, sc.end, sc.shift))
        return out

    def _directives(self, text, low, resolver, mentions, unmatched, sess, labels, times) -> Optional[str]:
        if not mentions and not unmatched:
            return None
        if not _EXAM_WORDS.search(low) and not sess and not re.search(r"\b(?:unavailab\w*|exclud\w*|restor\w*|available\s+again)\b", low):
            return None
        directives = nlu.parse_directives(text, resolver, mentions, labels, times, self.today(), self._year(),
                                          self.settings.admin_keywords, self.settings.leave_keywords,
                                          self.settings.default_year_window_days)
        if not directives and not unmatched:
            return None
        if not directives:
            return None
        if not sess:
            # no examination yet: only claim it when the coordinator clearly means the examination period
            if not re.search(r"\b(?:entire|complete|whole|(?:examination|exam)\s+(?:period|session)|session)\b", low):
                return None
        # validate identities first
        amb = [m for d in directives for m in d.mentions if m.resolved is None]
        if amb:
            return self._ambiguous(amb)
        asks = [d for d in directives if d.needs]
        if asks:
            who = ", ".join(sorted({m.text for d in asks for m in d.mentions}))
            return f"For {who} I also need {asks[0].needs}. Nothing was changed yet."
        total = sum(len(d.mentions) for d in directives)
        plan = []
        for d in directives:
            ids = [m.resolved for m in d.mentions]
            plan.append((d, ids))
        if total > self.settings.large_operation_threshold and not self.pending_confirm:
            self.pending_confirm = {"kind": "directives", "plan": plan, "unmatched": unmatched, "sid": sess["id"] if sess else None}
            return self._plan_table(plan, unmatched) + f"\n\nThis changes {total} people. Reply **confirm** to apply or **cancel**."
        return self._execute_plan(plan, unmatched, sess["id"] if sess else None)

    def _plan_table(self, plan, unmatched) -> str:
        rows = []
        for d, ids in plan:
            for fid in ids:
                f = self.repo.faculty(fid)
                for sc in d.scopes:
                    rows.append([f["name"], f["department"] or "—", d.action, describe_scope(
                        {"kind": sc.kind, "start_date": sc.start.isoformat() if sc.start else None,
                         "end_date": sc.end.isoformat() if sc.end else None, "shift_label": sc.shift}),
                        d.reason or "—"])
        txt = md_table(["Faculty", "Department", "Action", "Scope", "Reason"], rows)
        if unmatched:
            txt += "\n\nNot found in the faculty list (nothing applied): " + ", ".join(unmatched)
        return txt

    def _run_confirmed(self) -> str:
        pc, self.pending_confirm = self.pending_confirm, None
        if pc["kind"] == "directives":
            return self._execute_plan(pc["plan"], pc["unmatched"], pc["sid"])
        if pc["kind"] == "callback":
            return pc["run"]()
        if pc["kind"] == "import":
            return self.policy_chat.run_import(pc["probe"])
        return "Nothing to confirm."

    def _execute_plan(self, plan, unmatched, sid) -> str:
        created, errors, notes, lines = [], [], [], []
        if sid is None:
            # remembered until the examination is created
            for d, ids in plan:
                if d.action == "exclude":
                    self.pending_exclusions.append((d, ids))
            msg = "Noted for the next examination allocation:\n\n" + self._plan_table(plan, unmatched)
            return msg
        for d, ids in plan:
            if d.action == "exclude":
                res = self.svc.restrictions.add_bulk(self._specs(d, ids, sid))
                created += res.created
                errors += [(self.repo.faculty(s.faculty_id)["name"], e) for s, e in res.errors]
                notes += res.notes
                lines += [f"{self.repo.faculty(r['faculty_id'])['name']} - {describe_scope(r)}" for r in res.existing]
            else:
                sess_row = self.repo.session(sid)
                for fid in ids:
                    for sc in d.scopes:
                        if sc.start and sc.start > date.fromisoformat(sess_row["end_date"]):
                            notes.append(f"{self.repo.faculty(fid)['name']}: {sc.start:%d %b} is after the examination period "
                                         f"(ends {date.fromisoformat(sess_row['end_date']):%d %b}), so there is nothing to restore from then.")
                            continue
                        ch = self.svc.restrictions.restore(
                            fid, session_id=sid,
                            from_date=sc.start if sc.kind in ("date", "date_range", "shift") else None,
                            to_date=sc.end if sc.kind in ("date", "date_range", "shift") and sc.end else
                            (sc.start if sc.kind in ("date", "shift") else None),
                            shift_label=sc.shift, categories=d.categories, kinds=d.kinds)
                        name = self.repo.faculty(fid)["name"]
                        if not ch:
                            notes.append(f"{name}: no matching restriction to restore.")
                        for c in ch:
                            r = c["restriction"]
                            kept = "; still restricted: " + ", ".join(describe_scope(x) for x in c["remaining"]) if c["remaining"] else ""
                            created.append({"restored": True, "name": name, "scope": describe_scope(r), "kept": kept})
        out = []
        ex = [c for c in created if not c.get("restored")]
        rs = [c for c in created if c.get("restored")]
        if ex:
            out.append("**Restrictions recorded**\n" + md_table(
                ["#", "Faculty", "Department", "Scope", "Type", "Reason"],
                [[r["id"], self.repo.faculty(r["faculty_id"])["name"], self.repo.faculty(r["faculty_id"])["department"] or "—",
                  describe_scope(r), r["category"], r["reason"] or "—"] for r in ex]))
        if rs:
            out.append("**Eligibility restored**\n" + md_table(["Faculty", "Lifted", "Note"],
                                                              [[r["name"], r["scope"], r["kept"] or "nothing else changed"] for r in rs]))
        if lines:
            out.append("Already in place (not duplicated): " + "; ".join(lines))
        if errors:
            out.append("**Not applied**\n" + "\n".join(f"- {n}: {e}" for n, e in errors))
        if unmatched:
            out.append("Not found in the faculty list, so nothing was applied for: " + ", ".join(unmatched))
        out += notes
        out.append(self._after_change())
        return "\n\n".join(o for o in out if o)

    def _after_change(self) -> str:
        alloc = self._latest_alloc()
        if not alloc:
            return ""
        errs = self.svc.validate_allocation(alloc["id"])
        if errs:
            return (f"The current allocation (version {alloc['version']}) now conflicts with these rules in {len(errs)} place(s), "
                    f"e.g. {errs[0]}. Say *generate allocation again*, or *remove <name> from <date>* to fix individual duties.")
        return f"The current allocation (version {alloc['version']}) is still valid under these changes."

    # ---------------------------------------------------------------- parameters
    def _change_params(self, text: str, low: str) -> Optional[str]:
        sess = self._session()
        if self._is_question(low):
            return None
        req = nlu.parse_exam_request(text, self.today(), self._year(), shift_label=self.settings.shift_label_template)
        changes: Dict[str, Any] = {}
        if req.max_duties is not None and re.search(r"\bmax", low):
            changes["max_duties"] = req.max_duties
        if req.min_duties is not None and re.search(r"\bmin|at least", low):
            changes["min_duties"] = req.min_duties
        if req.per_room and re.search(r"\binvigilator", low):
            changes["invigilators_per_room"] = req.per_room
        if req.classes_suspended is not None:
            changes["classes_suspended"] = int(req.classes_suspended)
        opts: Dict[str, Any] = {}
        if req.prohibit_consecutive:
            opts["prohibit_consecutive"] = True
        if re.search(r"\ballow\b.{0,20}\bconsecutive", low):
            opts["prohibit_consecutive"] = None
        if req.max_per_day:
            opts["max_per_day"] = req.max_per_day
        if not changes and not opts:
            return None
        if self._latest_alloc() and self._latest_alloc()["status"] == "approved":
            note = " (the approved allocation stays in force until a new version is approved)"
        else:
            note = ""
        self.svc.update_session(sess["id"], **changes, **opts)
        return ("Updated the examination settings: " + ", ".join(f"{k.replace('_', ' ')} = {v}" for k, v in {**changes, **{k: v for k, v in opts.items()}}.items())
                + f"{note}. Say *generate allocation again* to apply them.")

    @staticmethod
    def _is_question(low: str) -> bool:
        return bool(re.match(r"^\W*(?:why|what|which|who|how|show|list|find|display|give|tell)\b", low))

    # ------------------------------------------------------------------- queries
    def _queries(self, text, low, mentions, sess) -> Optional[str]:
        alloc = self._latest_alloc() if re.search(r"\b(?:draft|latest|new)\b", low) else self._alloc()
        alloc = alloc or self._latest_alloc()
        names = [self.repo.faculty(m.resolved)["name"] for m in mentions if m.resolved]
        dp = nlu.DateParser(self.today(), self._year())
        hits = dp.find(text)
        on = hits[0].first if hits else None

        if re.search(r"\bexclusion|\brestriction|\bwho\b.{0,25}\b(?:excluded|unavailable)\b", low) and not re.search(r"\bwhy\b", low):
            return ("**Exclusions & unavailability**\n" + self.rep.render_exclusions(sess["id"], include_inactive=bool(re.search(r"\b(?:history|all|restored)\b", low)))
                    + "\n\n" + self.policy_chat._show_excluded(sess))
        if re.search(r"\b(?:who\s+is|list|show)\b.{0,30}\beligible|eligibility|why\b.{0,20}\b(?:excluded|not\s+eligible)\b", low) and not names:
            return "**Eligibility**\n" + self.rep.render_eligibility(sess["id"], only_problem=not re.search(r"\ball\b|\bevery", low))
        if alloc is None:
            if re.search(r"\b(?:allocation|duties|report|distribution)\b", low):
                return "There is no allocation yet. Describe the examination (dates, shifts, rooms, invigilators per room) and I will prepare one."
            return None
        aid = alloc["id"]
        if re.search(r"\bwhy\b", low):
            if len(names) >= 2 and re.search(r"\b(?:more|fewer|less|than|compared?|differ\w*)\b", low):
                return self.rep.compare(aid, names[0], names[1])
            if names and on:
                return self.rep.explain_assignment(aid, names[0], on)
            if names:
                return self.rep.explain_faculty(aid, names[0])
            if re.search(r"\bunfilled|\bnot\s+filled|\bempty", low):
                return self.rep.render_unfilled(aid)
        if re.search(r"\bunfilled|\bempty\s+(?:rooms?|seats?)|\bnot\s+(?:been\s+)?filled|\bshortage", low):
            return "**Unfilled rooms**\n" + self.rep.render_unfilled(aid)
        if re.search(r"\bconsecutive\b", low):
            rows = self.rep.consecutive_faculty(aid)
            if not rows:
                return "Nobody has duties on consecutive examination days in this allocation."
            return md_table(["Faculty", "Department", "Consecutive duty days"],
                            [[r["faculty"], r["department"], "; ".join(f"{fmt_date(a)} -> {fmt_date(b)}" for a, b in r["pairs"])]
                             for r in rows])
        if re.search(r"\b(?:not\s+(?:received|been\s+(?:given|assigned))|no\s+dut(?:y|ies)|zero\s+dut|without\s+(?:any\s+)?dut|"
                     r"haven'?t\s+(?:received|got))", low):
            rows = [r for r in self.rep.counts(aid) if r["new"] == 0]
            return md_table(["Faculty", "Department", "Status", "Reason"],
                            [[r["name"], r["department"], r["status"], r["why"] if r["eligible_slots"] == 0 else
                              "Eligible but not needed (fair share already covered)"] for r in rows])
        if re.search(r"\bgroup[- ]?wise|\bgroup\s+(?:split|distribution)|\bnon[- ]?[a-z]{2,}\b.{0,30}\b(?:distribution|duties|share|split)|"
                     r"\b(?:distribution|duties|share|split)\b.{0,30}\bnon[- ]?[a-z]{2,}\b", low):
            return "**Group split**\n" + self.rep.render_groups(aid)
        if re.search(r"\bdepartment[- ]?wise|\bby\s+department|\bper\s+department|\bdepartments?\b.{0,20}\b(?:distribution|duties|workload)", low):
            return "**Department-wise distribution**\n" + self.rep.render_departments(aid)
        if re.search(r"\bfairness|workload\s+(?:distribution|deviation)|\bstatistics\b", low):
            return "**Fairness report**\n" + self.rep.render_fairness(aid)
        if re.search(r"\b(?:full|complete|final)\s+report|\bgenerate\s+(?:the\s+)?report|\breport\b", low):
            return self.rep.render_full(aid)
        if re.search(r"\b(?:total|every|each|all)\b.{0,40}\b(?:duties|dut)|duty\s+(?:count|statistics|totals)|workload", low) and not names:
            return "**Faculty workload**\n" + self.rep.render_workload(aid)
        if names and re.search(r"\b(?:duties|duty|workload|assigned|schedule)\b", low):
            rows = [d for d in self.repo.duties(aid) if d["faculty"] == names[0]]
            c = next(r for r in self.rep.counts(aid) if r["name"] == names[0])
            head = f"**{names[0]}** - historical {num(c['historical'] + c['committed'])}, new {c['new']}, combined {num(c['combined'])}."
            return head + "\n\n" + md_table(["Date", "Shift", "Room", "Seat"],
                                            [[fmt_date(d["exam_date"]), d["shift"], d["room"], d["seat"]] for d in rows])
        if re.search(r"\b(?:show|display|view|preview|list)\b.{0,30}\b(?:allocation|roster|duties|schedule|rooms?)\b", low) or \
                (on and re.search(r"\ballocation|\bduties|\bschedule", low)):
            body = self.rep.render_schedule(aid, on) if on else self.rep.render_headline(aid) + "\n\n" + self.rep.render_schedule(aid, max_rows=self.settings.chat_schedule_max_rows)
            return body
        if re.search(r"\bsummary|\bstatus\b", low) and re.search(r"\b(?:exam\w*|allocation)\b", low):
            return self.rep.render_summary(aid)
        return None