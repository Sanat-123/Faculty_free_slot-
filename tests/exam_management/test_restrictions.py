"""Exclusion, availability and restoration (items 10-19 of the checklist)."""
from datetime import date

import pytest

from exam_management.restrictions import RestrictionError, RestrictionSpec

D = lambda d: date(2026, 11, d)


def assigned(world, aid, i):
    fid = world.fid(i)
    return [d for d in world.svc.repo.duties(aid) if d["faculty_id"] == fid]


def test_permanent_session_date_range_shift_exclusions_are_honoured(world):
    sid = world.session(days=6, shifts=__import__("tests.exam_management.dataset", fromlist=["x"]).SHIFTS_TWO, rooms=3)
    R = world.svc.restrictions
    R.add(RestrictionSpec(world.fid(0), "permanent", "retired from duty"))
    R.add(RestrictionSpec(world.fid(1), "session", "NAAC work", "administrative", sid))
    R.add(RestrictionSpec(world.fid(2), "date", "leave", "leave", sid, D(12)))
    R.add(RestrictionSpec(world.fid(3), "date_range", "conference", "leave", sid, D(12), D(14)))
    R.add(RestrictionSpec(world.fid(4), "shift", "meeting", "general", sid, D(11), None, "Morning"))
    out = world.svc.generate(sid, seed=1)
    aid = out.allocation_id
    assert world.svc.validate_allocation(aid) == []
    assert assigned(world, aid, 0) == [] and assigned(world, aid, 1) == []
    assert all(d["exam_date"] != "2026-11-12" for d in assigned(world, aid, 2))
    assert all(not ("2026-11-12" <= d["exam_date"] <= "2026-11-14") for d in assigned(world, aid, 3))
    assert all(not (d["exam_date"] == "2026-11-11" and d["shift"] == "Morning") for d in assigned(world, aid, 4))
    # the date-specific person stays eligible on other dates (not over-excluded)
    m = world.svc.eligibility.evaluate(sid)
    assert len(m.eligible_slots(world.fid(2))) == len(m.slots) - 2      # 2 shifts on 12 Nov
    assert len(m.eligible_slots(world.fid(4))) == len(m.slots) - 1


def test_bulk_exclusion_keeps_each_persons_scope_separate(world):
    sid = world.session()
    specs = [RestrictionSpec(world.fid(0), "session", "admin", "administrative", sid),
             RestrictionSpec(world.fid(1), "date", "leave", "leave", sid, D(12)),
             RestrictionSpec(world.fid(2), "date_range", "x", "general", sid, D(13), D(15))]
    res = world.svc.restrictions.add_bulk(specs)
    assert len(res.created) == 3 and not res.errors
    by = {r["faculty_id"]: r for r in res.created}
    assert by[world.fid(1)]["start_date"] == "2026-11-12" and by[world.fid(1)]["end_date"] is None
    assert by[world.fid(2)]["end_date"] == "2026-11-15"
    assert by[world.fid(0)]["start_date"] is None


def test_invalid_specs_are_rejected_without_blocking_valid_ones(world):
    sid = world.session()
    res = world.svc.restrictions.add_bulk([
        RestrictionSpec(world.fid(0), "date_range", "x", "general", sid, D(14), D(12)),   # reversed
        RestrictionSpec(world.fid(1), "date", "x", "general", sid, date(2026, 12, 25)),   # outside period
        RestrictionSpec(world.fid(2), "shift", "x", "general", sid, D(12), None, "Midnight"),  # no such shift
        RestrictionSpec(world.fid(3), "date", "ok", "general", sid, D(12))])
    assert len(res.errors) == 3 and len(res.created) == 1


def test_duplicates_are_not_created_twice(world):
    sid = world.session()
    spec = RestrictionSpec(world.fid(0), "date", "x", "general", sid, D(12))
    world.svc.restrictions.add(spec)
    _, created, _ = world.svc.restrictions.add(spec)
    assert created is False
    assert len(world.svc.restrictions.active(world.fid(0))) == 1


def test_overlapping_restrictions_coexist(world):
    sid = world.session()
    R = world.svc.restrictions
    R.add(RestrictionSpec(world.fid(0), "date_range", "a", "general", sid, D(11), D(13)))
    r, created, note = R.add(RestrictionSpec(world.fid(0), "date", "b", "leave", sid, D(12)))
    assert created and "already covered" in note
    assert len(R.active(world.fid(0))) == 2


def test_restore_only_removes_what_was_asked(world):
    sid = world.session()
    R = world.svc.restrictions
    R.add(RestrictionSpec(world.fid(0), "session", "admin", "administrative", sid))
    R.add(RestrictionSpec(world.fid(0), "date", "leave", "leave", sid, D(12)))
    ch = R.restore(world.fid(0), session_id=sid, categories=["administrative"])
    assert len(ch) == 1 and ch[0]["restriction"]["category"] == "administrative"
    left = R.active(world.fid(0))
    assert len(left) == 1 and left[0]["category"] == "leave"


def test_restore_from_a_date_narrows_instead_of_deleting(world):
    sid = world.session(days=7)
    R = world.svc.restrictions
    R.add(RestrictionSpec(world.fid(0), "session", "admin", "administrative", sid))
    R.restore(world.fid(0), session_id=sid, from_date=D(14))
    m = world.svc.eligibility.evaluate(sid)
    ok_days = {s.exam_date.day for s in m.slots if m.ok(world.fid(0), s.index)}
    assert ok_days == {14, 15, 16}
    hist = R.active(world.fid(0), include_inactive=True)
    assert any(r["status"] == "superseded" for r in hist)       # audit trail kept
    assert world.svc.repo.audit_log("restriction")


def test_restore_one_shift_keeps_the_other_shift_restricted(make_world):
    from tests.exam_management import dataset as ds
    w = make_world()
    sid = w.session(days=3, shifts=ds.SHIFTS_TWO)
    R = w.svc.restrictions
    R.add(RestrictionSpec(w.fid(0), "date", "leave", "leave", sid, D(11)))
    R.restore(w.fid(0), session_id=sid, from_date=D(11), to_date=D(11), shift_label="Afternoon")
    m = w.svc.eligibility.evaluate(sid)
    state = {s.shift_label: m.ok(w.fid(0), s.index) for s in m.slots if s.exam_date == D(11)}
    assert state == {"Morning": False, "Afternoon": True}


def test_bulk_restore(world):
    sid = world.session()
    R = world.svc.restrictions
    ids = [world.fid(i) for i in range(3)]
    for f in ids:
        R.add(RestrictionSpec(f, "session", "x", "general", sid))
    R.restore_bulk(ids, session_id=sid)
    assert all(not R.active(f) for f in ids)


def test_administrative_policy_is_configurable_not_assumed(world):
    sid = world.session()
    repo = world.svc.repo
    repo.add_responsibility(world.fid(0), "HOD")
    repo.add_responsibility(world.fid(1), "NAAC coordinator")
    repo.add_responsibility(world.fid(2), "Dean")
    # nothing excluded until the coordinator applies a policy
    assert world.svc.eligibility.evaluate(sid).eligible_slots(world.fid(0))
    out = world.svc.restrictions.apply_admin_policy(sid, roles=["hod", "dean"])
    assert len(out["created"]) == 2
    m = world.svc.eligibility.evaluate(sid)
    assert not m.eligible_slots(world.fid(0)) and not m.eligible_slots(world.fid(2))
    assert m.eligible_slots(world.fid(1))                       # role not selected
    assert "Administrative responsibility" in m.summary(world.fid(0))[1]
    out2 = world.svc.restrictions.apply_admin_policy(sid)       # all roles now
    assert len(out2["created"]) == 1 and len(out2["existing"]) == 2   # idempotent


def test_admin_policy_with_no_records_excludes_nobody(world):
    sid = world.session()
    out = world.svc.restrictions.apply_admin_policy(sid)
    assert out["none_found"] and not out["created"]


def test_responsibility_with_dates_only_excludes_overlap(world):
    sid = world.session(days=6)
    world.svc.repo.add_responsibility(world.fid(0), "Exam cell", start=D(12), end=D(30))
    world.svc.restrictions.apply_admin_policy(sid)
    m = world.svc.eligibility.evaluate(sid)
    assert {s.exam_date.day for s in m.slots if m.ok(world.fid(0), s.index)} == {10, 11}