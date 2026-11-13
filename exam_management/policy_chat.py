"""
Natural-language handling of department exclusion, allocation priority and file import.

Hybrid NLP, no fixed sentences:
  * INTENT   - scored from weighted cue families (exclude / restore / priority-low / priority-order /
               priority-reset / show / reallocate / import).  Cue words live in config/policy_lexicon.json,
               so synonyms and other languages are added without code changes.
  * ENTITIES - departments are found by n-gram lookup against the departments in the DATABASE
               (aliases derived from labels + administrator aliases); dates by the project's DateParser;
               shifts from the current session; a reason from "because / due to".
  * SAFETY   - an ambiguous department, a missing date or session is ASKED about, never guessed; anything
               that refers to a named person is left to the individual-exclusion handler.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import nlu
from .importer import SUPPORTED
from .policy import DepartmentResolver, PolicyError
from .reports import fmt_date, md_table


def _lexicon() -> Dict[str, Any]:
    env = os.environ.get("EXAM_MGMT_POLICY_LEXICON")
    path = Path(env) if env else Path(__file__).resolve().parent.parent / "config" / "policy_lexicon.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


@dataclass
class Found:
    label: str
    start: int
    end: int


class PolicyChat:

    def __init__(self, handler: Any):
        self.h = handler
        self.lex = _lexicon()
        self._cue = {k: re.compile(v, re.I) for k, v in self.lex.get("cues", {}).items()}

    # ------------------------------------------------------------ entities
    def find_departments(self, text: str) -> Tuple[List[Found], List[Tuple[str, List[str]]]]:
        resolver = self.h.svc.policy.resolver()
        toks = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[A-Za-z0-9]+", text)]
        found: List[Found] = []
        ambiguous: List[Tuple[str, List[str]]] = []
        used = [False] * len(toks)
        dept_words = set(self.lex.get("department_words", ["department", "dept", "faculty", "teachers", "staff", "branch"]))
        for n in (5, 4, 3, 2, 1):
            for i in range(len(toks) - n + 1):
                if any(used[i:i + n]):
                    continue
                words = [t[0] for t in toks[i:i + n]]
                key = " ".join(w.lower() for w in words)
                owners = resolver.alias_map.get(key)
                if not owners:
                    continue
                if n == 1 and len(key) <= 3:                 # "it", "me", "ce": only when written as an abbreviation
                    after = toks[i + 1][0].lower() if i + 1 < len(toks) else ""
                    if not (words[0].isupper() or after in dept_words):
                        continue
                if n == 1 and key in set(self.lex.get("not_department_words", [])):
                    continue
                for j in range(i, i + n):
                    used[j] = True
                if len(owners) == 1:
                    found.append(Found(next(iter(owners)), toks[i][1], toks[i + n - 1][2]))
                else:
                    ambiguous.append((" ".join(words), sorted(owners)))
        if not found and not ambiguous:                       # misspelling: one fuzzy try on unused words
            stop = set(self.lex.get("stop_words", []))
            for i, (w, a, b) in enumerate(toks):
                if used[i] or len(w) < 6 or w.lower() in stop:
                    continue
                r = resolver.resolve(w)
                if r.ok and r.how in ("fuzzy", "prefix"):
                    found.append(Found(r.label, a, b))        # type: ignore[arg-type]
        seen, uniq = set(), []
        for f in sorted(found, key=lambda x: x.start):
            if f.label not in seen:
                seen.add(f.label)
                uniq.append(f)
        return uniq, ambiguous

    def _scope(self, text: str, low: str, sess: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """kind/dates/shift/reason from the sentence."""

        labels, _ = self.h._shift_info()
        shift = next((lb for lb in labels if re.search(rf"\b{re.escape(lb.lower())}\b", low)), None)
        dp = nlu.DateParser(self.h.today(), self.h._year())
        hits = dp.find(text)
        reason = ""
        m = re.search(r"\b(?:because(?:\s+of)?|due\s+to|owing\s+to|since)\b\s+(.+?)(?:[.;]|$)", text, re.I)
        if m:
            reason = m.group(1).strip()
        start = end = None
        if hits:
            h0 = hits[0]
            start, end = h0.first, (h0.last if h0.is_range else None)
            if len(hits) > 1 and not h0.is_range:             # "10 November and 12 November" -> two single dates
                start, end = h0.first, None
        if self._cue.get("permanent") and self._cue["permanent"].search(low):
            kind = "permanent"
        elif shift and start:
            kind = "shift"
        elif shift and sess:
            kind, start, end = "shift", date.fromisoformat(sess["start_date"]), date.fromisoformat(sess["end_date"])
        elif start and end:
            kind = "date_range"
        elif start:
            kind = "date"
        else:
            kind = "session"
        extra_dates = [h.first for h in hits[1:] if not h.is_range] if len(hits) > 1 and not hits[0].is_range else []
        return {"kind": kind, "start": start, "end": end, "shift": shift, "reason": reason, "extra_dates": extra_dates}

    # -------------------------------------------------------------- intents
    def _intent(self, low: str) -> Optional[str]:
        has = {k: bool(rx.search(low)) for k, rx in self._cue.items()}
        if has.get("import") and re.search(r"\b(?:import|upload)\s+(?:folder|directory)\b|\bscan\b.{0,20}\b(?:folder|directory)\b", low):
            return "scan"
        if has.get("import") and re.search(r"\.(?:" + "|".join(e.lstrip(".") for e in SUPPORTED) + r")\b", low):
            return "import"
        if has.get("accept_unlisted"):
            return "accept_unlisted"
        if has.get("link_names") and (has.get("unverified") or re.search(r"\bsame\b|\blink\b|\bmerge\b", low)) and not has.get("exclude"):
            return "link_names"
        if has.get("unverified") and (has.get("show") or not has.get("exclude")):
            return "unverified"
        if has.get("reject"):
            return "reject"
        if has.get("publish"):
            return "publish"
        if has.get("notify") or has.get("go_live"):
            return "notify"
        if has.get("export") and not has.get("exclude"):
            return "export"
        if has.get("overview"):
            return "overview"
        if has.get("show_priority"):
            return "show_priority"
        if has.get("show") and has.get("excluded_noun") and has.get("department_noun"):
            return "show_excluded"
        if has.get("priority_reset") and has.get("priority_noun"):
            return "priority_reset"
        if has.get("priority_order"):
            return "priority_order"
        if has.get("priority_low") or (has.get("priority_noun") and has.get("priority_last")):
            return "priority_low"
        if has.get("priority_high"):
            return "priority_high"
        if has.get("restore"):
            return "restore"
        if has.get("exclude"):
            return "exclude"
        if has.get("reallocate"):
            return "reallocate"
        return None

    # ---------------------------------------------------------------- entry
    def handle(self, text: str, low: str, mentions: List[Any], sess: Optional[Dict[str, Any]]) -> Optional[str]:
        intent = self._intent(low)
        if intent is None:
            return None
        if intent == "import":
            return self._import(text)
        if intent == "scan":
            return self._scan()
        if intent in ("show_excluded",):
            return self._show_excluded(sess)
        if intent == "show_priority":
            return self._show_priority(sess)
        if intent == "export":
            return self._export(low, sess)
        if intent == "unverified":
            return self._unverified()
        if intent == "link_names":
            return self._link_names(text)
        if intent == "accept_unlisted":
            return self._accept_unlisted(text)
        if intent in ("publish", "reject", "notify"):
            return self._workflow(intent, text, low, sess)
        if intent == "overview":
            if not sess:
                return "Set up the examination first (dates, shifts, rooms), then I can show who is eligible."
            from .exports import ReportExporter
            return ReportExporter(self.h.svc).render_overview(sess["id"])
        if intent == "reallocate" and not self.find_departments(text)[0]:
            return self._reallocate(sess, "")
        depts, amb = self.find_departments(text)
        if mentions and not re.search(r"\b(?:department|dept)s?\b", low):
            return None                                         # a named person: the individual handler owns it
        if amb:
            ph, cands = amb[0]
            return f"'{ph}' could mean " + " or ".join(cands) + ". Which one do you mean? Nothing was changed."
        if not depts:
            if intent in ("exclude", "restore") and re.search(r"\b(?:department|dept)s?\b", low):
                known = "; ".join(self.h.svc.policy.resolver().departments) or "none loaded yet"
                return f"I could not tell which department you mean. Known departments: {known}."
            return None
        names = [d.label for d in depts]
        if intent == "exclude":
            return self._exclude(text, low, names, sess)
        if intent == "restore":
            return self._restore(text, low, names, sess)
        if intent == "reallocate":
            ex = self._exclude(text, low, names, sess) if self._cue["exclude"].search(low) else ""
            return ex or self._reallocate(sess, "")
        return self._priority(intent, text, low, depts, sess)

    # ------------------------------------------------------------- actions
    def _exclude(self, text, low, names, sess) -> str:
        sc = self._scope(text, low, sess)
        policy = self.h.svc.policy
        if sess is None and sc["kind"] != "permanent":
            for n in names:
                self.h.pending_dept.append({"op": "exclude", "department": n, **sc})
            return ("Noted: I will exclude " + ", ".join(names) + " from the examination as soon as it is set up "
                    f"({self._scope_text(sc)}).")
        lines, created = [], 0
        for n in names:
            for st in [sc["start"]] + list(sc["extra_dates"]):
                row = policy.exclude_department(n, kind=sc["kind"], session_id=sess["id"] if sess else None,
                                                start=st if sc["kind"] != "session" else None,
                                                end=sc["end"] if st == sc["start"] else None,
                                                shift_label=sc["shift"], reason=sc["reason"])
                created += 0 if row.get("already_active") else 1
                lines.append(f"- **{n}**: {'already excluded' if row.get('already_active') else 'excluded'} "
                             f"({self._scope_text(sc, st)})")
                if sc["kind"] in ("session", "permanent"):
                    break
        n_people = sum(1 for f in self.h.repo.faculty_all() if (f["department"] or "") in names)
        out = ["Department exclusion recorded:", *lines,
               f"This covers {n_people} active faculty member(s). Individual exclusions are unaffected."]
        return "\n".join(out) + self._after(sess)

    def _restore(self, text, low, names, sess) -> str:
        sc = self._scope(text, low, sess)
        windowed = sc["kind"] in ("date", "date_range")
        lines = []
        for n in names:
            res = self.h.svc.policy.restore_department(
                n, session_id=sess["id"] if sess else None,
                start=sc["start"] if windowed else None, end=sc["end"] if windowed else None)
            lines.append(f"- **{n}**: " + ("had no active exclusion, so nothing changed" if not res["restored"] and not res["unchanged_open_ended"]
                                         else f"{len(res['restored'])} exclusion(s) lifted") +
                         (f"; {len(res['unchanged_open_ended'])} open-ended one(s) left as they are" if res["unchanged_open_ended"] else ""))
        return ("Department eligibility restored:\n" + "\n".join(lines) +
                "\nIndividual faculty exclusions were not touched. Say *generate allocation again* to use the restored people.")

    def _priority(self, intent, text, low, depts: List[Found], sess) -> str:
        pol = self.h.svc.policy
        sid = sess["id"] if sess else None
        rules = {r["pattern"].lower(): r for r in pol.priority_rules(sid) if r["scope"] == "department"}
        names = [d.label for d in depts]

        def tier(n: str) -> int:
            return rules[n.lower()]["tier"] if n.lower() in rules else 1

        if intent == "priority_reset":
            n = sum(pol.remove_priority("department", d, sid) for d in names)
            return f"Priority rule removed for {', '.join(names)} ({n} rule(s)); they use the default priority again." + self._after(sess, regen=True)
        if intent == "priority_order":
            if len(depts) < 2:
                return "Tell me both departments, e.g. 'give X priority over Y'."
            hi, lo = self._order(text, depts)
            if tier(lo) > tier(hi):
                return f"No change: {hi} already has priority over {lo} (tiers {tier(hi)} and {tier(lo)})."
            new = tier(hi) + 1
            pol.set_priority("department", lo, new, session_id=sid, label=f"after {hi}")
            return (f"Done: **{hi}** (priority {tier(hi)}) now comes before **{lo}** (priority {new}). "
                    f"{lo} faculty are used only when higher-priority faculty cannot fill the seats." + self._after(sess, regen=True))
        if intent == "priority_low":
            top = max([r["tier"] for r in rules.values()] + [2])
            out = []
            for n in names:
                cur = tier(n)
                if cur >= top and n.lower() in rules:
                    out.append(f"{n} is already at the lowest priority (priority {cur}).")
                    continue
                new = max(3, top + (0 if n.lower() in rules and cur == top else 1)) if not rules else top + (0 if cur == top else 1)
                pol.set_priority("department", n, new, session_id=sid, label="lowest priority")
                out.append(f"**{n}** is now priority {new}: used only when higher-priority faculty cannot fill the seats "
                           f"(still eligible - this is not an exclusion).")
            return "\n".join(out) + self._after(sess, regen=True)
        # priority_high
        out = []
        for n in names:
            pol.set_priority("department", n, 1, session_id=sid, label="first choice")
            out.append(f"**{n}** is now priority 1 (first choice).")
        others = [r for r in rules.values()]
        known = self.h.svc.policy.resolver().departments
        pair = [d for d in known if d not in names][:1]
        eg = f"'give {names[0]} priority over {pair[0]}'" if pair else "'give one department priority over another'"
        hint = "" if others else (" Other departments are also priority 1; tell me which departments it should rank above, "
                                  f"e.g. {eg}.")
        return "\n".join(out) + hint + self._after(sess, regen=True)

    def _order(self, text: str, depts: List[Found]) -> Tuple[str, str]:
        """(higher, lower) from 'X over Y', 'X before Y', 'prefer X to Y', 'Y after X', 'Y below X'."""

        a, b = depts[0], depts[1]
        between = text[a.end:b.start].lower()
        before = text[:a.start].lower()
        if re.search(r"\b(?:after|below|behind|lower\s+than|less\s+than|under)\b", between):
            return b.label, a.label
        if re.search(r"\b(?:over|before|above|ahead\s+of|higher\s+than|than|to)\b", between) or \
                re.search(r"\b(?:prefer|favou?r|prioriti[sz]e|give)\b", before):
            return a.label, b.label
        return a.label, b.label

    def _reallocate(self, sess, reason: str) -> str:
        alloc = self.h._latest_alloc()
        if not sess or not alloc:
            return "There is no allocation to repair yet."
        return self.h._present(self.h.svc.reallocate_affected(alloc["id"], reason=reason or "requested"), [])

    def _after(self, sess, regen: bool = False) -> str:
        alloc = self.h._latest_alloc() if sess else None
        if not alloc:
            return ""
        if regen:
            return "\n\nSay *generate allocation again* to apply this priority to the current draft."
        if alloc["status"] == "draft":
            out = self.h.svc.reallocate_affected(alloc["id"], reason="department exclusion")
            hint = ("\n\nOnly the affected seats were changed (everyone else kept their duty). "
                    "Say *generate allocation again* for a fresh optimisation of all duties.") if out.allocation_id else ""
            return "\n\n" + self.h._present(out, []) + hint
        return (f"\n\nThe approved allocation (version {alloc['version']}) is unchanged. Say *reallocate duties* to prepare "
                f"a replacement draft - it will need your approval before it takes effect.")

    def _workflow(self, intent: str, text: str, low: str, sess) -> str:
        from .notifications import NotificationService
        alloc = self.h._latest_alloc() if sess else None
        if not alloc:
            return "There is no allocation yet."
        ns = getattr(self.h, "notifier", None) or NotificationService(self.h.svc)
        self.h.notifier = ns
        aid, ver = alloc["id"], alloc["version"]
        if intent == "reject":
            m = re.search(r"\b(?:because|due\s+to|reason\s*[:\-]?)\s*(.+)$", text, re.I)
            if alloc["status"] != "draft":
                return f"Version {ver} is {alloc['status']}; only a draft can be rejected."
            if not m:
                return "Please give a reason (for example: 'reject this draft because room 5 is overloaded') - it is kept in the audit trail."
            ns.reject(aid, m.group(1).strip())
            return f"Draft version {ver} rejected and kept in the audit trail. Say *generate allocation again* for a revised draft."
        if intent == "publish":
            if alloc["status"] != "approved":
                return (f"Version {ver} is {alloc['status']}. Only an approved allocation can be published - "
                        f"review and approve it first.")
            if ns.is_published(aid):
                return f"Version {ver} is already published."
            self.h.pending_confirm = {"kind": "callback", "run": lambda: (
                ns.publish(aid) and f"Version {ver} is published. Faculty notifications can now be prepared ('send notifications').")}
            return f"Publish version {ver} to the faculty? This makes it the official allocation. Reply **confirm** or **cancel**."
        # notify
        if not ns.is_published(aid):
            return "Notifications are only prepared for a published allocation. Approve and publish it first."
        ns.plan(aid)
        dry = ns.dispatch(aid, dry_run=True)
        st = ns.status(aid)
        live = bool(self._cue["go_live"].search(low))
        base = (f"Prepared notifications (dry run - nothing sent). Status: {st}. {dry['would_send']} message(s) are waiting "
                f"to be sent through connected providers: {sorted(ns.providers) or 'none connected'}.")
        if not live or not dry["would_send"]:
            return base + ("" if not dry["would_send"] else "\nSay *send for real* to dispatch them.")
        self.h.pending_confirm = {"kind": "callback", "run": lambda: (
            lambda r: f"Dispatched: {r['sent']} accepted by the provider, {r['delivered']} confirmed delivered, "
                      f"{r['failed']} failed. 'Accepted' is not the same as delivered; check the log for details.")(
            ns.dispatch(aid, dry_run=False, confirmed=True))}
        return base + f"\nSend {dry['would_send']} real message(s) now? Reply **confirm** or **cancel**."

    # ----------------------------------------------- names not on the imported roster
    def _titles(self) -> List[str]:
        from .importer import load_columns
        return load_columns().get("titles", [])

    def _clean_name(self, text: str) -> str:
        from .importer import strip_titles
        return strip_titles(re.sub(r"[\"'`.]", " ", text), self._titles())

    def _best(self, phrase: str, pool: List[Dict[str, Any]]):
        """(match, ambiguous_candidates): the single best name in `pool`, or the close rivals when unsure."""
        from rapidfuzz import fuzz, process
        from .importer import strip_titles
        key = self._clean_name(phrase)
        if not key:
            return None, []
        labels = {i: strip_titles(re.sub(r"\s*\((?:EC|unverified)[^)]*\)", "", p["name"]), self._titles()) for i, p in enumerate(pool)}
        scored = sorted(((max(fuzz.token_sort_ratio(key, v), fuzz.token_set_ratio(key, v) - 8), i) for i, v in labels.items()), reverse=True)
        scored = [(sc, i) for sc, i in scored if sc >= 80][:4]
        if not scored:
            return None, []
        if len(scored) == 1 or scored[0][0] - scored[1][0] >= 6 or scored[0][0] >= 99:
            return pool[scored[0][1]], []
        return None, [pool[i] for _, i in scored]

    def _unverified(self) -> str:
        imp = self.h.svc.importer
        parked = imp.unverified()
        if not parked:
            return "Nobody is waiting for verification: every active name is on the imported roster."
        roster = [f for f in self.h.repo.faculty_all() if f.get("record_status") not in ("needs_verification", "merged")]
        rows = []
        for p in parked:
            match, _ = self._best(p["name"], roster)
            rows.append([p["name"], p["department"] or "—", match["name"] if match else "no close match on the roster"])
        return ("**Names that are NOT on the imported roster** (set aside: they get no duties until you decide; nothing was deleted)\n"
                + md_table(["Name from the timetable", "Department", "Closest roster name"], rows) +
                "\n- Same person spelled differently? Say *link <timetable name> to <roster name>*.\n"
                "- A real, eligible person who is missing from the roster? Say *accept <name>*.")

    def _split_link(self, text: str):
        for pat in (r"\b(?:link|merge|match|connect)\s+(.+?)\s+(?:to|with|and|into)\s+(.+)$",
                    r"^(.+?)\s+(?:is|are)\s+(?:the\s+)?same(?:\s+person)?\s+as\s+(.+)$"):
            m = re.search(pat, text.strip().rstrip(".?!"), re.I)
            if m:
                return m.group(1).strip(), m.group(2).strip()
        return None

    def _link_names(self, text: str) -> str:
        parts = self._split_link(text)
        if not parts:
            return "Tell me both names, e.g. *link <timetable name> to <roster name>*."
        left, right = parts
        imp = self.h.svc.importer
        parked = imp.unverified()
        roster = [f for f in self.h.repo.faculty_all() if f.get("record_status") not in ("needs_verification", "merged")]
        a, amb_a = self._best(left, parked)
        b, amb_b = self._best(right, roster)
        for who, amb in ((left, amb_a), (right, amb_b)):
            if amb:
                return f"'{who}' could be " + " or ".join(f"{c['name']}" for c in amb) + ". Which one? Nothing was changed."
        if not a:
            return f"'{left}' is not in the list of unverified names. Say *show unverified faculty* to see them."
        if not b:
            return f"I could not find '{right}' on the roster. Say *show unverified faculty* for the closest names."
        self.h.pending_confirm = {"kind": "callback", "run": lambda: (
            lambda r: f"Linked: '{a['name']}' is now treated as {b['name']}. Their timetable classes now count as teaching "
                      f"clashes for {b['name']}." if r["status"] == "linked" else "Could not link them.")(
            imp.confirm_alias(a["id"], b["id"]))}
        return (f"Treat the timetable name **{a['name']}** as the roster person **{b['name']}** "
                f"({b['department'] or 'no department listed'})? Reply **confirm** or **cancel**.")

    def _accept_unlisted(self, text: str) -> str:
        who = re.sub(r"^\W*(?:please\s+)?accept\s+", "", text.strip(), flags=re.I).rstrip(".?!")
        who = re.sub(r"\s+(?:as\s+(?:eligible|a\s+faculty.*)|for\s+(?:exam|duty).*)$", "", who, flags=re.I)
        imp = self.h.svc.importer
        a, amb = self._best(who, imp.unverified())
        if amb:
            return f"'{who}' could be " + " or ".join(c["name"] for c in amb) + ". Which one? Nothing was changed."
        if not a:
            return None  # not one of the parked names: leave 'accept ...' to the other handlers
        self.h.pending_confirm = {"kind": "callback", "run": lambda: (
            imp.accept_unlisted(a["id"]) and f"{a['name']} is accepted as eligible for examination duty.")}
        return (f"Accept **{a['name']}** as a real, eligible faculty member even though they are not on the imported roster? "
                f"Reply **confirm** or **cancel**.")

    def _export(self, low: str, sess) -> str:
        alloc = self.h._latest_alloc() if sess else None
        if not alloc:
            return "There is no allocation to report on yet. Generate one first."
        from .exports import ReportExporter
        ex = ReportExporter(self.h.svc)
        cfg = self.h.svc.settings
        folder = Path(os.environ.get("EXAM_MGMT_EXPORT_DIR") or cfg.export_dir)
        folder.mkdir(parents=True, exist_ok=True)
        from datetime import date as _d
        stem = cfg.export_name_template.format(session=sess["id"], version=alloc["version"], date=_d.today().isoformat())
        want_pdf = bool(re.search(r"\bpdf\b", low)) or not re.search(r"\b(?:excel|xlsx|spreadsheet)\b", low)
        want_xls = bool(re.search(r"\b(?:excel|xlsx|spreadsheet)\b", low)) or not re.search(r"\bpdf\b", low)
        made = []
        if want_pdf:
            made.append(str(ex.to_pdf(alloc["id"], folder / f"{stem}.pdf")))
        if want_xls:
            made.append(str(ex.to_excel(alloc["id"], folder / f"{stem}.xlsx")))
        self.h.last_exports = made
        status = alloc["status"]
        return ("Reports A-E generated from the database (" + ("DRAFT - not approved or published" if status == "draft" else status) +
                "):\n" + "\n".join(f"- {m}" for m in made))

    # ------------------------------------------------------------- displays
    @staticmethod
    def _scope_text(sc: Dict[str, Any], st: Optional[date] = None) -> str:
        shift = f" ({sc['shift']} shift)" if sc.get("shift") else ""
        if sc["kind"] == "permanent":
            return "permanently"
        if sc["kind"] == "session":
            return "whole examination" + shift
        a = st or sc["start"]
        if sc["kind"] == "date_range" and sc["end"]:
            return f"{fmt_date(a)} to {fmt_date(sc['end'])}{shift}"
        return f"{fmt_date(a)}{shift}"

    def _show_excluded(self, sess) -> str:
        rows = self.h.svc.policy.department_restrictions(sess["id"] if sess else None, active_only=False)
        if not rows:
            return "No department is excluded."
        def span(r):
            a, b = r["start_date"], r["end_date"]
            return {"permanent": "permanent", "session": "whole examination"}.get(r["kind"]) or \
                   (f"{a} to {b}" if b else a) + (f" ({r['shift_label']})" if r["shift_label"] else "")
        return "**Excluded departments**\n" + md_table(
            ["Department", "Scope", "Reason", "Status", "Created by"],
            [[r["department"], span(r), r["reason"] or "—", r["status"], r["created_by"] or "—"] for r in rows])

    def _show_priority(self, sess) -> str:
        sid = sess["id"] if sess else None
        rules = self.h.svc.policy.priority_rules(sid)
        if not rules:
            return "No priority rules are set: every department is priority 1 (equal)."
        return "**Priority rules** (1 = first choice; higher numbers are used only when lower ones cannot fill the seats)\n" + \
            md_table(["Applies to", "Match", "Priority", "Scope", "Note"],
                     [[r["scope"], r["pattern"], r["tier"], "this examination" if r["session_id"] else "all examinations",
                       r["label"] or "—"] for r in rules])

    # ---------------------------------------------------------------- import
    def _scan(self) -> str:
        found = self.h.svc.importer.scan_directory()
        if not found:
            return ("No import folder is configured or it holds no supported files. Set EXAM_MGMT_IMPORT_DIR (or upload a file "
                    "in the administrator panel); supported: PDF, XLSX, XLS, CSV.")
        rows = [[f["file"], f["purpose"].replace("_", " "), f["rows"], "already imported" if f["already_imported"] else
                 ("needs your confirmation" if f["purpose"] == "unknown" else "ready")] for f in found]
        return "**Import folder**\n" + md_table(["File", "Detected as", "Rows", "State"], rows) + \
               "\nTell me which file to import (e.g. 'import faculty data from <path>') and I will show a preview first."

    def _import(self, text: str) -> str:
        m = re.search(r"([^\s\"']+\.(?:" + "|".join(e.lstrip(".") for e in SUPPORTED) + r"))", text, re.I)
        path = m.group(1) if m else ""
        if not path or not Path(path).is_file():
            return (f"I could not find a file at '{path or '(no path given)'}'. Upload it in the administrator panel, or give "
                    f"me its full path (PDF, XLSX, XLS or CSV).")
        imp = self.h.svc.importer
        probe = imp.probe(path)
        pv = probe.preview(3)
        if probe.purpose == "unknown":
            return ("I could not tell what this file contains.\n" + "\n".join(f"- {p}" for p in probe.problems) +
                    "\nTell me whether it is a faculty roster or a duty-count sheet and which columns hold the name, "
                    "department, employee code and count.")
        val = imp.validate(probe)
        self.h.pending_confirm = {"kind": "import", "probe": probe}
        cols = "; ".join(f"{f} ← '{c}'" for f, c in next(iter(pv["columns"].values()), {}).items())
        return (f"**File preview** - {probe.filename}\n- Detected: **{probe.purpose.replace('_', ' ')}** "
                f"({probe.confidence:.0%} sure), {probe.row_count} rows\n- Columns: {cols}\n"
                f"- Validation: {val['errors']} error(s), {val['warnings']} warning(s)\n"
                f"- Sample: {pv['sample'][:2]}\n\nReply **confirm** to import it, or **cancel**.")

    def run_import(self, probe) -> str:
        res = self.h.svc.importer.import_probe(probe)
        self.h.refresh_roster()
        if res["status"] != "imported":
            return res.get("message", f"Import not done: {res['status']}")
        parts = [f"Imported {probe.filename} (import #{res['import_id']})."]
        if "roster" in res:
            r = res["roster"]
            parts.append(f"Faculty: {r['added']} new, {r['updated']} updated, {r['unchanged']} unchanged; "
                         f"{r['not_in_this_file']} earlier roster record(s) are not in this file (kept, not deleted)."
                         + (f" {r['parked_not_on_roster']} name(s) that are not on the roster were set aside (not deleted)."
                            if r.get("parked_not_on_roster") else ""))
        if "history" in res:
            h = res["history"]
            parts.append(f"Historical duties: {h['applied']} applied ({h['total_duties']:g} in total), "
                         f"{h['not_applied']} not applied; older totals for the same people were superseded, not deleted.")
        if res.get("issues"):
            parts.append(f"{res['issues']} row issue(s) were logged for review (nothing was silently dropped).")
        return " ".join(parts)