"""Fairness, constraints, randomisation, reallocation, history, concurrency."""
import pathlib
import threading
from collections import Counter, defaultdict
from datetime import date

import pytest

from exam_management.config import ExamSettings
from exam_management.providers import StaticDirectory, StaticTimetable
from exam_management.restrictions import RestrictionSpec
from exam_management.service import ExamService, ExamError, MissingInformation, SessionSpec
from tests.exam_management import dataset as ds
from tests.exam_management.conftest import FAST, START, World

D = lambda d: date(2026, 11, d)


def pairs(world, aid):
    return {(d["faculty_id"], d["exam_date"], d["shift_id"]) for d in world.svc.repo.duties(aid)}


def new_counts(world, aid):
    return Counter(d["faculty_id"] for d in world.svc.repo.duties(aid))


# ---------------------------------------------------------------- 1-2 colleges
def test_every_department_is_considered(world):
    sid = world.session(days=6, rooms=8)
    aid = world.svc.generate(sid, seed=3).allocation_id
    depts = {d["department"] for d in world.svc.repo.duties(aid)}
    assert depts == set(world.svc.repo.departments()) and len(depts) == 6


def test_unknown_new_department_is_picked_up_without_code_changes(make_world):
    rows = ds.roster(8, depts=["Astro Basket Weaving", "Underwater Welding", "Quantum Gardening"])
    w = make_world(rows)
    aid = w.svc.generate(w.session(days=4, rooms=5), seed=1).allocation_id
    assert {d["department"] for d in w.svc.repo.duties(aid)} == {"Astro Basket Weaving", "Underwater Welding",
                                                                 "Quantum Gardening"}


def test_no_department_or_person_names_are_hardcoded_in_the_package():
    src = "\n".join(p.read_text() for p in pathlib.Path("exam_management").glob("*.py"))
    import re
    for word in ds.DEPTS + ds.FIRST + ds.LAST:
        assert not re.search(rf"\b{re.escape(word)}\b", src), f"'{word}' is hard-coded in exam_management"


# ------------------------------------------------------------------ 3-8 fairness
def test_history_influences_fairness_but_is_not_the_only_input(make_world):
    rows = ds.roster(10)
    w = make_world(rows)
    sid = w.session(days=6, rooms=6, mx=5)
    aid = w.svc.generate(sid, seed=2).allocation_id
    n = new_counts(w, aid)
    ranked = sorted(w.ids.values(), key=lambda f: w.hist[w.svc.repo.faculty(f)["name"]])
    low, high = ranked[:15], ranked[-15:]
    assert sum(n[f] for f in low) / 15 > sum(n[f] for f in high) / 15
    # combined workload is more even than history alone
    import statistics
    comb = [w.hist[w.svc.repo.faculty(f)["name"]] + n[f] for f in w.ids.values()]
    assert statistics.pstdev(comb) < statistics.pstdev(w.hist.values())


def test_current_and_historical_counts_are_kept_separate(world):
    sid = world.session()
    aid = world.svc.generate(sid, seed=1).allocation_id
    from exam_management.reports import Reporter
    rows = {r["name"]: r for r in Reporter(world.svc).counts(aid)}
    for name, r in rows.items():
        assert r["historical"] == world.hist[name]
        assert r["combined"] == r["historical"] + r["new"]


def test_lowest_history_person_is_not_given_every_day(make_world):
    rows = ds.roster(10)
    w = make_world(rows)
    lowest = min(w.hist, key=w.hist.get)
    w.svc.repo.import_history({**w.hist, lowest: 0.0}, "duty_sheet")
    for n in w.names:
        if n != lowest:
            w.svc.repo.import_history({}, "noop")
    sid = w.session(days=10, rooms=6, mx=4)
    aid = w.svc.generate(sid, seed=4).allocation_id
    days = {d["exam_date"] for d in w.svc.repo.duties(aid) if d["faculty"] == lowest}
    assert 0 < len(days) <= 4 and len(days) < 10


def test_maximum_duty_limit_is_never_exceeded(world):
    sid = world.session(days=8, rooms=8, mx=3)
    for seed in range(4):
        aid = world.svc.generate(sid, seed=seed).allocation_id
        assert max(new_counts(world, aid).values()) <= 3
        assert world.svc.validate_allocation(aid) == []


def test_minimum_is_met_when_feasible_and_shortfall_reported_when_not(make_world):
    w = make_world(ds.roster(10))
    sid = w.session(days=8, rooms=8, mn=2, mx=4)               # 128 seats for 60 people -> all reach 2
    aid = w.svc.generate(sid, seed=1).allocation_id
    q = w.svc.repo.allocation(aid)["quality"]
    assert q.get("below_minimum") == [] and min(new_counts(w, aid).values()) >= 2
    sid2 = w.session(days=2, rooms=2, mn=2, mx=4)               # 8 seats for 60 people -> cannot
    aid2 = w.svc.generate(sid2, seed=1).allocation_id
    q2 = w.svc.repo.allocation(aid2)["quality"]
    assert len(q2["below_minimum"]) > 0
    assert len(w.svc.repo.duties(aid2)) == 8                    # no artificial extra duties


def test_consecutive_duties_are_minimised_and_can_be_forbidden(make_world):
    w = make_world(ds.roster(10))
    sid = w.session(days=8, rooms=4, mx=4)
    aid = w.svc.generate(sid, seed=1).allocation_id
    assert w.svc.repo.allocation(aid)["quality"]["fairness"]["consecutive_pairs"] == 0
    sid2 = w.session(days=8, rooms=8, mx=5, prohibit_consecutive=True)
    aid2 = w.svc.generate(sid2, seed=1).allocation_id
    assert w.svc.repo.allocation(aid2)["quality"]["fairness"]["consecutive_pairs"] == 0
    assert w.svc.validate_allocation(aid2) == []


def test_eligibility_scaled_targets_do_not_force_equal_duties(make_world):
    w = make_world(ds.roster(10))
    sid = w.session(days=10, rooms=5, mx=5)
    for i in range(10):                                         # these 10 are free for 3 days only
        w.svc.restrictions.add(RestrictionSpec(w.fid(i), "date_range", "away", "leave", sid, D(13), D(19)))
    aid = w.svc.generate(sid, seed=2).allocation_id
    n = new_counts(w, aid)
    limited = [n[w.fid(i)] for i in range(10)]
    full = [n[w.fid(i)] for i in range(10, 60)]
    assert max(limited) <= 3                                    # only 3 eligible days
    assert sum(limited) / 10 < sum(full) / 50
    for d in w.svc.repo.duties(aid):
        if d["faculty_id"] in [w.fid(i) for i in range(10)]:
            assert d["exam_date"] <= "2026-11-12" or d["exam_date"] == "2026-11-20"


# ---------------------------------------------------- 21-22 class suspension
def test_class_suspension_decides_whether_the_timetable_matters(make_world, tmp_path):
    busy = StaticTimetable({"Tuesday": [(ds.roster(10)[0][0], "08:00", "13:00")]})   # 10 Nov 2026 is a Tuesday
    w = make_world(timetable=busy)
    teaching = w.names[0]
    sid_susp = w.session(days=2, rooms=2, suspended=True)
    sid_norm = w.session(days=2, rooms=2, suspended=False)
    m1 = w.svc.eligibility.evaluate(sid_susp)
    m2 = w.svc.eligibility.evaluate(sid_norm)
    tue1 = [s.index for s in m1.slots if s.exam_date.weekday() == 1][0]
    assert m1.ok(w.ids[teaching], tue1)                          # classes suspended: timetable ignored
    assert not m2.ok(w.ids[teaching], tue1)                      # classes running: conflict honoured
    assert "class" in m2.reasons(w.ids[teaching], tue1)[0].text.lower()


def test_suspension_must_be_stated_not_assumed(world):
    sid = world.svc.create_session(SessionSpec("X", ds.days(START, 2), ds.SHIFTS_ONE, per_room=2, room_count=2,
                                               classes_suspended=None))
    with pytest.raises(MissingInformation) as e:
        world.svc.generate(sid, seed=1)
    assert "classes_suspended" in e.value.fields


# --------------------------------------------------------- 23-24 randomisation
def test_another_allocation_is_different_valid_and_equally_fair(world):
    sid = world.session(days=6, rooms=6)
    a = world.svc.generate(sid, seed=1)
    b = world.svc.generate(sid, seed=2, avoid_previous=True)
    assert b.allocation_id and b.status == "complete"
    assert pairs(world, a.allocation_id) != pairs(world, b.allocation_id)
    assert world.svc.validate_allocation(b.allocation_id) == []
    qa, qb = a.result.quality["score"], b.result.quality["score"]
    assert qb <= qa * (1 + FAST.variation_quality_tolerance) + 1


def test_same_seed_reproduces_and_different_seeds_vary_without_breaking_rules(world):
    sid = world.session(days=6, rooms=6)
    a = world.svc.generate(sid, seed=11)
    b = world.svc.generate(sid, seed=11)
    assert pairs(world, a.allocation_id) == pairs(world, b.allocation_id)
    seen = set()
    for seed in (21, 22, 23):
        o = world.svc.generate(sid, seed=seed)
        assert world.svc.validate_allocation(o.allocation_id) == []
        seen.add(frozenset(pairs(world, o.allocation_id)))
    assert len(seen) > 1


def test_no_different_allocation_is_explained_instead_of_inferior_one(make_world):
    rows = ds.roster(2, depts=["A", "B"])        # 4 faculty, 4 seats -> a single valid allocation
    w = make_world(rows)
    sid = w.session(days=1, rooms=2, mx=1)
    first = w.svc.generate(sid, seed=1)
    again = w.svc.generate(sid, seed=2, avoid_previous=True)
    assert again.status == "unchanged" and again.allocation_id is None
    assert "No different allocation" in again.notes[0]


# ------------------------------------------------------- 25-26 reallocation
def test_reallocation_replaces_only_the_affected_seat(world):
    sid = world.session(days=6, rooms=6)
    a = world.svc.generate(sid, seed=1)
    world.svc.approve(a.allocation_id)
    victim = [d for d in world.svc.repo.duties(a.allocation_id) if d["exam_date"] == "2026-11-12"][0]
    out = world.svc.replace_duty(a.allocation_id, victim["faculty_id"], D(12), reason="urgent meeting")
    assert out.status == "complete"
    assert len(out.changes) == 1 and out.changes[0]["old"] == victim["faculty"]
    new_pairs = pairs(world, out.allocation_id)
    assert (victim["faculty_id"], "2026-11-12", victim["shift_id"]) not in new_pairs
    kept = pairs(world, a.allocation_id) - {(victim["faculty_id"], "2026-11-12", victim["shift_id"])}
    assert kept <= new_pairs                                     # every other assignment preserved
    assert world.svc.validate_allocation(out.allocation_id) == []
    # the approved version stays in force until the revision is approved
    assert world.svc.repo.allocation(a.allocation_id)["status"] == "approved"
    assert world.svc.repo.allocation(out.allocation_id)["status"] == "draft"
    # the removal is recorded so a later full run respects it
    again = world.svc.generate(sid, seed=9)
    assert (victim["faculty_id"], "2026-11-12") not in {(d["faculty_id"], d["exam_date"]) for d in
                                                        world.svc.repo.duties(again.allocation_id)}


def test_replacement_respects_maximum_and_never_picks_ineligible(make_world):
    w = make_world(ds.roster(4, depts=["A", "B", "C"]))          # 12 faculty
    sid = w.session(days=3, rooms=3, per=2, mx=2)                # 18 seats, 12*2 = 24 capacity
    a = w.svc.generate(sid, seed=1)
    victim = w.svc.repo.duties(a.allocation_id)[0]
    out = w.svc.replace_duty(a.allocation_id, victim["faculty_id"], date.fromisoformat(victim["exam_date"]))
    assert w.svc.validate_allocation(out.allocation_id) == []
    assert max(Counter(d["faculty_id"] for d in w.svc.repo.duties(out.allocation_id)).values()) <= 2


def test_failed_replacement_is_reported_as_unfilled_not_hidden(make_world):
    w = make_world(ds.roster(2, depts=["A", "B"]))               # 4 faculty exactly fill 2 rooms x 2
    sid = w.session(days=1, rooms=2, per=2, mx=1)
    a = w.svc.generate(sid, seed=1)
    victim = w.svc.repo.duties(a.allocation_id)[0]
    out = w.svc.replace_duty(a.allocation_id, victim["faculty_id"], D(10))
    assert out.status == "partial" and w.svc.repo.unfilled(out.allocation_id)


# ------------------------------------------------- 27 insufficient faculty
def test_insufficient_faculty_reports_unfilled_rooms_and_blocks_approval(make_world):
    w = make_world(ds.roster(3, depts=["A", "B"]))               # 6 faculty
    sid = w.session(days=1, rooms=5, per=2, mx=4)                # needs 10
    out = w.svc.generate(sid, seed=1)
    assert out.status == "partial"
    un = w.svc.repo.unfilled(out.allocation_id)
    assert len(un) == 4 and all("eligible" in u["reason"] for u in un)
    assert len(w.svc.repo.duties(out.allocation_id)) == 6
    res = w.svc.approve(out.allocation_id)
    assert res["approved"] is False and "PARTIAL" in " ".join(res["problems"]).upper()
    from exam_management.reports import Reporter
    assert "unfilled" in Reporter(w.svc).render_summary(out.allocation_id).lower() or \
        "PARTIAL" in Reporter(w.svc).render_summary(out.allocation_id)
    assert w.svc.approve(out.allocation_id, allow_partial=True)["approved"]


def test_hard_maximum_can_leave_rooms_unfilled_with_a_reason(make_world):
    w = make_world(ds.roster(3, depts=["A", "B"]))
    sid = w.session(days=3, rooms=3, per=2, mx=1)                # 18 seats, capacity 6
    out = w.svc.generate(sid, seed=1)
    assert out.status == "partial" and len(w.svc.repo.duties(out.allocation_id)) == 6
    assert any("maximum duty limit" in u["reason"] for u in w.svc.repo.unfilled(out.allocation_id))


# ------------------------------------------------------ 28-31 database / history
def test_records_are_saved_and_versions_kept(world):
    sid = world.session()
    a = world.svc.generate(sid, seed=1)
    b = world.svc.generate(sid, seed=2)
    repo = world.svc.repo
    assert len(repo.duties(b.allocation_id)) == 72
    versions = repo.allocations(sid)
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["status"] == "superseded" and versions[1]["status"] == "draft"
    assert repo.audit_log("allocation")


def test_draft_and_cancelled_duties_never_count_as_history(world):
    sid = world.session()
    a = world.svc.generate(sid, seed=1)
    comps = world.svc.repo.history_components()
    assert all(c["completed"] == 0 and c["committed"] == 0 for c in comps.values())   # draft
    world.svc.approve(a.allocation_id)
    other = world.session()
    p, _ = world.svc.build_problem(other)
    assert any(f.committed > 0 for f in p.faculty) and all(f.history == world.hist[f.name] for f in p.faculty)
    world.svc.cancel_draft  # (approved cannot be discarded)
    with pytest.raises(ExamError):
        world.svc.cancel_draft(a.allocation_id)
    b = world.svc.generate(sid, seed=2)
    world.svc.cancel_draft(b.allocation_id)
    assert world.svc.repo.allocation(b.allocation_id)["status"] == "cancelled"
    assert {d["status"] for d in world.svc.repo.duties(b.allocation_id)} == {"cancelled"}


def test_history_is_not_double_counted(world):
    sid = world.session()
    before = world.svc.repo.history_components()
    for _ in range(3):
        world.svc.repo.import_history(world.hist, "duty_sheet")          # re-import same sheet
        world.svc.generate(sid, seed=1)                                   # regenerate
    after = world.svc.repo.history_components()
    assert before == after
    a = world.svc.generate(sid, seed=5)
    world.svc.approve(a.allocation_id)
    n = world.svc.complete_session(sid)
    comps = world.svc.repo.history_components()
    total_hist = sum(c["baseline"] + c["completed"] for c in comps.values())
    assert total_hist == sum(world.hist.values()) + n == sum(world.hist.values()) + 72
    again = world.svc.repo.history_components()
    assert again == comps                                                 # idempotent


def test_unknown_names_in_history_import_are_reported_not_guessed(world):
    out = world.svc.repo.import_history({"Dr. Nobody Here": 5.0, world.names[0]: 3.0}, "other_sheet")
    assert out["unknown"] == ["Dr. Nobody Here"] and out["imported"] == 1


def test_repeated_requests_never_create_duplicate_duties(world):
    import sqlite3
    sid = world.session()
    a = world.svc.generate(sid, seed=1)
    d = world.svc.repo.duties(a.allocation_id)[0]
    with pytest.raises(sqlite3.IntegrityError):
        world.svc.conn.execute(
            "INSERT INTO exm_duty(allocation_id,faculty_id,exam_date,shift_id,room,seat) VALUES (?,?,?,?,?,?)",
            (a.allocation_id, d["faculty_id"], d["exam_date"], d["shift_id"], "Room 99", 1))
    for _ in range(3):
        world.svc.generate(sid, seed=1)
    keys = [(x["faculty_id"], x["exam_date"], x["shift_id"]) for x in
            world.svc.repo.duties(world.svc.repo.latest_allocation(sid)["id"])]
    assert len(keys) == len(set(keys))
    open_drafts = [v for v in world.svc.repo.allocations(sid) if v["status"] == "draft"]
    assert len(open_drafts) == 1


def test_migration_is_safe_to_repeat(tmp_path):
    from exam_management import db
    c = db.connect(tmp_path / "m.db")
    assert db.migrate(c) == [1, 2, 3] and db.migrate(c) == []
    c.execute("INSERT INTO exm_faculty(name,norm,updated_at) VALUES ('A','a','now')")
    assert db.migrate(c) == [] and c.execute("SELECT COUNT(*) FROM exm_faculty").fetchone()[0] == 1


# -------------------------------------------------------------- 32 concurrency
def test_concurrent_generation_leaves_one_open_draft_and_valid_rows(tmp_path):
    path = tmp_path / "conc.db"
    rows = ds.roster(10)
    w = World(path, rows)
    sid = w.session(days=5, rooms=5)
    results, errors = [], []

    def work(seed):
        try:
            svc = ExamService(str(path), directory=StaticDirectory(rows), settings=FAST)
            results.append(svc.generate(sid, seed=seed).allocation_id)
        except Exception as e:                                    # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=work, args=(s,)) for s in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    allocs = w.svc.repo.allocations(sid)
    assert len([a for a in allocs if a["status"] == "draft"]) == 1
    for a in allocs:
        if a["status"] == "draft":
            assert w.svc.validate_allocation(a["id"]) == []
        dups = w.svc.conn.execute("SELECT COUNT(*) - COUNT(DISTINCT faculty_id||exam_date||shift_id) FROM exm_duty "
                                  "WHERE allocation_id=?", (a["id"],)).fetchone()[0]
        assert dups == 0


def test_concurrent_approvals_never_double_book_a_person(tmp_path):
    path = tmp_path / "conc2.db"
    rows = ds.roster(10)
    w = World(path, rows)
    s1, s2 = w.session(days=3, rooms=4), w.session(days=3, rooms=4)          # same dates!
    a1, a2 = w.svc.generate(s1, seed=1).allocation_id, w.svc.generate(s2, seed=2).allocation_id
    outcome = []

    def approve(aid):
        svc = ExamService(str(path), directory=StaticDirectory(rows), settings=FAST)
        outcome.append(svc.approve(aid, allow_partial=True)["approved"])

    ts = [threading.Thread(target=approve, args=(a,)) for a in (a1, a2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    booked = defaultdict(int)
    for a in w.svc.repo.allocations(s1) + w.svc.repo.allocations(s2):
        if a["status"] == "approved":
            for d in w.svc.repo.duties(a["id"]):
                booked[(d["faculty_id"], d["exam_date"], d["shift_id"])] += 1
    assert all(v == 1 for v in booked.values())
    assert any(outcome)                                           # at least one approval succeeded