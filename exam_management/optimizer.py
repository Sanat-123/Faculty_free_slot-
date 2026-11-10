"""
CP-SAT allocation engine - pure data in (models.Problem), pure data out
(models.SolveResult).  No database, no chatbot.

DECISION VARIABLE   x[f, s] = 1  <=>  faculty f invigilates in slot s (a date + shift)
                    (rooms are filled afterwards from the people chosen for the slot;
                     this is equivalent to x[f, room, date, shift] because every room
                     of a slot needs the same number of invigilators, and keeps the
                     model small enough for 100 faculty x 10 days x 20 rooms.)

HARD CONSTRAINTS    * a seat is never over-filled; empty seats are REPORTED, not faked
                    * nobody in two overlapping slots
                    * only (faculty, slot) pairs the eligibility engine allowed
                      (exclusions, unavailability, shifts, existing duties ...)
                    * maximum duties per faculty (and per day when configured)
                    * no consecutive days when the coordinator forbids them
                    * fixed assignments (kept approved duties) stay

SOFT OBJECTIVES     (weights: config.OptimizerWeights - one place only)
                    empty seats, distance from the fair workload target, shortfall
                    from the minimum, consecutive days / too little rest, repeated
                    shifts on a day, concentration, moving kept assignments,
                    repeating the previous version, plus a small random cost that
                    only breaks ties between comparable candidates.

CONTROLLED RANDOMNESS  the random cost is tiny next to every fairness term, so it only
                    reorders candidates whose suitability is (nearly) equal.  The same
                    seed + the same data gives the same allocation.
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ortools.sat.python import cp_model

from .config import ExamSettings, OptimizerWeights
from .eligibility import overlapping_pairs
from .fairness import compute_targets, fairness_metrics
from .models import FacultyInfo, Problem, SlotInfo, SolveResult


class OptimizerError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# quality (what the objective measures, minus randomness / variation terms)
# ---------------------------------------------------------------------------

def _tier_capacity(problem: Problem, members: Sequence[FacultyInfo]) -> float:
    """How many seats this set of people can cover at most (per-person limits and per-slot seats)."""

    ids = {f.id for f in members}
    per_person = sum(min(len(problem.eligible.get(f.id, ())), problem.max_duties
                         if problem.max_duties is not None else 10 ** 9) for f in members)
    per_slot = sum(min(sl.required, sum(1 for i in ids if sl.index in problem.eligible.get(i, ())))
                   for sl in problem.slots)
    return float(min(per_person, per_slot))


def targets_for(problem: Problem, settings: ExamSettings) -> Dict[int, int]:
    """Fair per-person targets; with a group split they are computed inside each group.
    With priority tiers, demand is filled tier by tier: a lower tier only gets what the higher
    tiers cannot cover."""

    scale = settings.target_scale
    kw = dict(history_weight=settings.history_weight, history_cap=settings.history_adjust_cap,
              history_cap_fraction=settings.history_adjust_fraction)
    tiers = sorted({f.tier for f in problem.faculty})
    if not problem.groups and len(tiers) > 1:
        out: Dict[int, int] = {}
        remaining = float(problem.total_required)
        for t in tiers:
            members = [f for f in problem.faculty if f.tier == t]
            cap_t = _tier_capacity(problem, members)
            share = min(remaining, cap_t)
            out.update(compute_targets(members, {f.id: problem.eligible.get(f.id, set()) for f in members},
                                       int(round(share)), problem.max_duties, settings.opportunity_blend, scale,
                                       problem.min_duties if t == tiers[0] else None, demand_cap_by_slot=cap_t, **kw))
            remaining -= share
        return out
    if not problem.groups:
        return compute_targets(problem.faculty, problem.eligible, problem.total_required, problem.max_duties,
                               settings.opportunity_blend, scale, problem.min_duties, **kw)
    out = {}
    names = sorted(set(problem.groups.values()))
    for g in names:
        members = [f for f in problem.faculty if problem.groups.get(f.id) == g]
        demand = sum(q.get(g, 0) for q in problem.group_quota.values())
        out.update(compute_targets(members, {f.id: problem.eligible.get(f.id, set()) for f in members}, demand,
                                   problem.max_duties, settings.opportunity_blend, scale, problem.min_duties, **kw))
    for f in problem.faculty:                         # people outside every group: no target
        out.setdefault(f.id, 0)
    return out


def group_counts(problem: Problem, assign: Dict[int, Sequence[int]]) -> Dict[int, Dict[str, int]]:
    out: Dict[int, Dict[str, int]] = {}
    for s, fids in assign.items():
        d: Dict[str, int] = defaultdict(int)
        for f in fids:
            g = problem.groups.get(f)
            if g is not None:
                d[g] += 1
        out[s] = d
    return out


def coef(weight: int, scale: int) -> int:
    """Integer coefficient for a term measured in 1/scale duty units squared / linear."""

    return max(1, round(weight / scale))


def evaluate(problem: Problem, assign: Dict[int, Sequence[int]], targets: Dict[int, int],
             settings: ExamSettings) -> Dict[str, Any]:
    w = settings.weights
    scale = settings.target_scale
    counts: Dict[int, int] = defaultdict(int)
    per_day: Dict[Tuple[int, int], int] = defaultdict(int)
    days_of: Dict[int, Set[int]] = defaultdict(set)
    slot_by_index = {s.index: s for s in problem.slots}
    for s, fids in assign.items():
        for f in fids:
            counts[f] += 1
            d = slot_by_index[s].day_pos
            per_day[(f, d)] += 1
            days_of[f].add(d)
    unfilled = sum(max(0, slot_by_index[s].required - len(assign.get(s, ()))) for s in slot_by_index)
    dev = sum((counts[f.id] * scale - targets.get(f.id, 0)) ** 2 for f in problem.faculty
              if problem.eligible.get(f.id))
    short = 0
    if problem.min_duties:
        for f in problem.faculty:
            cap = len(problem.eligible.get(f.id, ()))
            if cap:
                short += max(0, min(problem.min_duties, cap) - counts[f.id])
    cons = rest = 0
    pref = settings.rest_days_preferred
    for f, days in days_of.items():
        ds = sorted(days)
        for a, b in zip(ds, ds[1:]):
            g = b - a
            if g == 1:
                cons += 1
            elif g - 1 < pref:
                rest += pref - (g - 1)
    same = sum(v - 1 for v in per_day.values() if v > 1)
    gbal = 0
    if problem.groups:
        gc = group_counts(problem, assign)
        for s, quota in problem.group_quota.items():
            for g, q in quota.items():
                gbal += abs(gc.get(s, {}).get(g, 0) - q)
    conc = sum(max(0, counts[f.id] * scale - targets.get(f.id, 0) - scale) for f in problem.faculty
               if problem.eligible.get(f.id))
    tier_of = {f.id: f.tier for f in problem.faculty}
    tier_use = sum((tier_of.get(f, 1) - 1) * c for f, c in counts.items())
    parts = {
        "unfilled_seats": unfilled,
        "tier_use": tier_use * w.tier_penalty,
        "deviation": dev * coef(w.target_deviation, scale),
        "min_shortfall": short * w.min_shortfall,
        "consecutive": cons * w.consecutive_day,
        "insufficient_rest": rest * w.insufficient_rest,
        "same_day_repeat": same * w.same_day_repeat,
        "concentration": conc * coef(w.concentration, scale),
        "group_balance": gbal * w.group_balance,
    }
    total = parts["unfilled_seats"] * w.unfilled_slot + sum(v for k, v in parts.items() if k != "unfilled_seats")
    return {"score": int(total), "components": parts, "shortfall_people": short,
            "consecutive_pairs": cons}


# ---------------------------------------------------------------------------
# rooms
# ---------------------------------------------------------------------------

def assign_rooms(slot: SlotInfo, fids: Sequence[int], dept_of: Dict[int, str],
                 rng: random.Random) -> List[Tuple[str, int, int]]:
    """Rooms are staffed one by one; people of one department are spread over rooms."""

    per, rooms = slot.per_room, list(slot.rooms)
    n = len(fids)
    if not rooms or per <= 0 or n == 0:
        return []
    full, rem = divmod(n, per)
    used = full + (1 if rem else 0)
    positions = [(ri, seat) for seat in range(per) for ri in range(used)
                 if ri < full or seat < rem]
    groups: Dict[str, List[int]] = defaultdict(list)
    for f in fids:
        groups[dept_of.get(f, "")].append(f)
    ordered = sorted(groups.values(), key=lambda g: (-len(g), rng.random()))
    flat: List[int] = []
    for g in ordered:
        rng.shuffle(g)
        flat.extend(g)
    return [(rooms[ri], seat + 1, flat[i]) for i, (ri, seat) in enumerate(positions)]



# ---------------------------------------------------------------------------
# constructive start (fast, fairness-scored, randomised among comparable candidates)
# ---------------------------------------------------------------------------

def greedy_construct(problem: Problem, settings: ExamSettings, targets: Dict[int, int],
                     seed: int) -> Dict[int, List[int]]:
    """
    Fill slots in date order.  For each seat the marginal cost of giving the duty to
    each eligible person is computed with the SAME terms as the objective (distance
    to target, consecutive days, rest, repeats, minimum) plus a scarcity bonus for
    people who will soon run out of opportunities.  Among candidates whose cost is
    within `comparable_cost_epsilon` of the best, the pick is random (seeded).
    """

    w, scale = settings.weights, settings.target_scale
    rng = random.Random(seed)
    pref = settings.rest_days_preferred
    day_cap = problem.max_per_day or settings.max_shifts_per_day
    slots = sorted(problem.slots, key=lambda s: (s.day_pos, s.start, s.index))
    overlap_of: Dict[int, Set[int]] = defaultdict(set)
    for a, b in (problem.overlaps or overlapping_pairs(problem.slots)):
        overlap_of[a].add(b)
        overlap_of[b].add(a)

    allowed: Dict[int, Set[int]] = {
        f.id: {s for s in problem.eligible.get(f.id, ()) if (f.id, s) not in problem.forbidden}
        for f in problem.faculty}
    order = {s.index: i for i, s in enumerate(slots)}
    future_left: Dict[int, int] = {fid: len(v) for fid, v in allowed.items()}

    n: Dict[int, int] = defaultdict(int)
    days: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    mine: Dict[int, Set[int]] = defaultdict(set)
    assign: Dict[int, List[int]] = {s.index: [] for s in slots}
    c_dev, c_conc = coef(w.target_deviation, scale), coef(w.concentration, scale)
    gcnt: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def put(fid: int, sl: SlotInfo) -> None:
        g0 = problem.groups.get(fid)
        if g0 is not None:
            gcnt[sl.index][g0] += 1
        assign[sl.index].append(fid)
        n[fid] += 1
        days[fid][sl.day_pos] += 1
        mine[fid].add(sl.index)

    for fid, si in sorted(problem.fixed):
        if si in allowed.get(fid, ()):
            put(fid, next(s for s in slots if s.index == si))

    for sl in slots:
        for fid in allowed:
            if sl.index in allowed[fid]:
                future_left[fid] -= 1          # from here on: slots AFTER this one
        need = sl.required - len(assign[sl.index])
        for _ in range(max(0, need)):
            scored: List[Tuple[float, int]] = []
            here = set(assign[sl.index])
            for f in problem.faculty:
                fid = f.id
                if sl.index not in allowed[fid] or fid in here:
                    continue
                if problem.max_duties is not None and n[fid] >= problem.max_duties:
                    continue
                if any(o in mine[fid] for o in overlap_of[sl.index]):
                    continue
                if day_cap and days[fid][sl.day_pos] >= day_cap:
                    continue
                if problem.prohibit_consecutive and \
                        (days[fid].get(sl.day_pos - 1) or days[fid].get(sl.day_pos + 1)):
                    continue
                t, k = targets.get(fid, 0), n[fid]
                cost = (f.tier - 1) * w.tier_penalty
                cost += c_dev * (((k + 1) * scale - t) ** 2 - (k * scale - t) ** 2)
                cost += c_conc * (max(0, (k + 1) * scale - t - scale) - max(0, k * scale - t - scale))
                for d2, cnt in days[fid].items():
                    if cnt <= 0:
                        continue
                    g = abs(sl.day_pos - d2)
                    if g == 0:
                        cost += w.same_day_repeat
                    elif g == 1:
                        cost += w.consecutive_day
                    elif g - 1 < pref:
                        cost += w.insufficient_rest * (pref - (g - 1))
                if problem.min_duties and k < min(problem.min_duties, len(allowed[fid])):
                    cost -= w.min_shortfall
                # scarcity: people with few chances left must be used while they can
                want = max(0, -(-t // scale) - k - 1)
                if want > 0 and future_left[fid] <= want:
                    cost -= 3 * c_dev * scale * scale
                if (fid, sl.index) in problem.keep_preferred:
                    cost -= w.change_from_baseline
                if (fid, sl.index) in problem.previous:
                    cost += w.repeat_previous
                gname = problem.groups.get(fid)
                if gname is not None and sl.index in problem.group_quota:
                    cost += (-w.group_balance if gcnt[sl.index][gname] < problem.group_quota[sl.index].get(gname, 0)
                             else w.group_balance)
                scored.append((cost + rng.random() * w.jitter, fid))
            if not scored:
                break
            scored.sort()
            best = scored[0][0]
            pool = [fid for c, fid in scored if c <= best + settings.comparable_cost_epsilon]
            pick = rng.choice(pool) if len(pool) > 1 else scored[0][1]
            put(pick, sl)
    return assign


# ---------------------------------------------------------------------------
# local search: swap one person for another in a seat while the objective improves
# ---------------------------------------------------------------------------

def local_search(problem: Problem, settings: ExamSettings, targets: Dict[int, int],
                 assign: Dict[int, List[int]], seed: int, budget_s: float) -> Dict[int, List[int]]:
    """Incremental hill climbing on the same cost terms (hard constraints re-checked per move)."""

    import time
    w, scale, pref = settings.weights, settings.target_scale, settings.rest_days_preferred
    c_dev, c_conc = coef(w.target_deviation, scale), coef(w.concentration, scale)
    day_cap = problem.max_per_day or settings.max_shifts_per_day
    rng = random.Random(seed ^ 0xA11CE)
    slot_by = {s.index: s for s in problem.slots}
    overlap_of: Dict[int, Set[int]] = defaultdict(set)
    for a, b in (problem.overlaps or overlapping_pairs(problem.slots)):
        overlap_of[a].add(b)
        overlap_of[b].add(a)
    allowed = {f.id: {s for s in problem.eligible.get(f.id, ()) if (f.id, s) not in problem.forbidden}
               for f in problem.faculty}
    cap_f = {fid: len(v) for fid, v in allowed.items()}
    tier_by = {f.id: f.tier for f in problem.faculty}
    assign = {s: list(v) for s, v in assign.items()}
    n: Dict[int, int] = defaultdict(int)
    days: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    mine: Dict[int, Set[int]] = defaultdict(set)
    for s, fids in assign.items():
        for f in fids:
            n[f] += 1
            days[f][slot_by[s].day_pos] += 1
            mine[f].add(s)

    def fcost(fid: int, k: int, dmap: Dict[int, int]) -> int:
        t = targets.get(fid, 0)
        cost = c_dev * (k * scale - t) ** 2 + c_conc * max(0, k * scale - t - scale)
        cost += k * (tier_by.get(fid, 1) - 1) * w.tier_penalty
        if problem.min_duties:
            cost += w.min_shortfall * max(0, min(problem.min_duties, cap_f.get(fid, 0)) - k)
        ds = sorted(d for d, c in dmap.items() if c > 0)
        for d, c in dmap.items():
            if c > 1:
                cost += w.same_day_repeat * (c - 1)
        for i, a in enumerate(ds):
            for b in ds[i + 1:]:
                g = b - a
                if g == 1:
                    cost += w.consecutive_day
                elif g - 1 < pref:
                    cost += w.insufficient_rest * (pref - (g - 1))
                else:
                    break
        return cost

    def with_day(dmap, day, delta):
        m = dict(dmap)
        m[day] = m.get(day, 0) + delta
        return m

    gc: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s, fids in assign.items():
        for f in fids:
            if f in problem.groups:
                gc[s][problem.groups[f]] += 1

    def gdelta(s: int, a: int, b: int) -> int:
        ga, gb = problem.groups.get(a), problem.groups.get(b)
        if ga == gb or s not in problem.group_quota:
            return 0
        q, d = problem.group_quota[s], 0
        if gb is not None:
            d += abs(gc[s][gb] + 1 - q.get(gb, 0)) - abs(gc[s][gb] - q.get(gb, 0))
        if ga is not None:
            d += abs(gc[s][ga] - 1 - q.get(ga, 0)) - abs(gc[s][ga] - q.get(ga, 0))
        return d * w.group_balance

    fixed = problem.fixed
    seats = [(s, f) for s, fids in assign.items() for f in fids if (f, s) not in fixed]
    deadline = time.monotonic() + budget_s
    improved = True
    while improved and time.monotonic() < deadline:
        improved = False
        rng.shuffle(seats)
        for idx, (s, a) in enumerate(list(seats)):
            if time.monotonic() > deadline:
                break
            if a not in assign[s]:
                continue
            sl = slot_by[s]
            ca = fcost(a, n[a], days[a])
            ca_new = fcost(a, n[a] - 1, with_day(days[a], sl.day_pos, -1))
            best_gain, best_b = 0, None
            here = set(assign[s])
            for f in problem.faculty:
                b = f.id
                if b in here or s not in allowed[b]:
                    continue
                if problem.max_duties is not None and n[b] >= problem.max_duties:
                    continue
                if any(o in mine[b] for o in overlap_of[s]):
                    continue
                if day_cap and days[b].get(sl.day_pos, 0) >= day_cap:
                    continue
                if problem.prohibit_consecutive and (days[b].get(sl.day_pos - 1) or days[b].get(sl.day_pos + 1)):
                    continue
                cb = fcost(b, n[b], days[b])
                cb_new = fcost(b, n[b] + 1, with_day(days[b], sl.day_pos, +1))
                delta = (ca_new - ca) + (cb_new - cb) + gdelta(s, a, b)
                if (b, s) in problem.previous:
                    delta += w.repeat_previous
                if (a, s) in problem.previous:
                    delta -= w.repeat_previous
                if (a, s) in problem.keep_preferred:
                    delta += w.change_from_baseline
                if (b, s) in problem.keep_preferred:
                    delta -= w.change_from_baseline
                gain = -delta
                if gain > best_gain + settings.comparable_cost_epsilon // 2:
                    best_gain, best_b = gain, b
            if best_b is not None:
                b = best_b
                assign[s].remove(a)
                assign[s].append(b)
                if a in problem.groups:
                    gc[s][problem.groups[a]] -= 1
                if b in problem.groups:
                    gc[s][problem.groups[b]] += 1
                n[a] -= 1
                days[a][sl.day_pos] -= 1
                mine[a].discard(s)
                n[b] += 1
                days[b][sl.day_pos] += 1
                mine[b].add(s)
                seats[idx] = (s, b)
                improved = True
        # exchange moves: two people trade seats in different slots (counts stay the same)
        def ok_move(f: int, s_new: int, s_old: int) -> bool:
            if s_new not in allowed[f] or f in assign[s_new]:
                return False
            if any(o in (mine[f] - {s_old}) for o in overlap_of[s_new]):
                return False
            dm = with_day(with_day(days[f], slot_by[s_old].day_pos, -1), slot_by[s_new].day_pos, +1)
            dn = slot_by[s_new].day_pos
            if day_cap and dm.get(dn, 0) > day_cap:
                return False
            if problem.prohibit_consecutive and (dm.get(dn - 1) or dm.get(dn + 1)):
                return False
            return True

        base_cost: Dict[int, int] = {f: fcost(f, n[f], days[f]) for f in n}
        flat = [(s, f) for s, fids in assign.items() for f in fids if (f, s) not in fixed]
        pen = [(s, f) for s, f in flat if fcost(f, n[f], days[f]) > fcost(f, n[f], {}) or
               any(abs(d1 - d2) < 2 for d1 in days[f] for d2 in days[f] if d1 < d2)]
        rng.shuffle(pen)
        for s1, a in pen:
            if time.monotonic() > deadline:
                break
            if a not in assign[s1]:
                continue
            d1 = slot_by[s1].day_pos
            best = (0, None)
            for s2, b in flat:
                if s2 == s1 or b == a or b not in assign[s2] or a in assign[s2] or b in assign[s1]:
                    continue
                if problem.groups.get(a) != problem.groups.get(b):
                    continue                      # exchanges keep every slot's group counts unchanged
                d2 = slot_by[s2].day_pos
                if d1 == d2 and False:
                    continue
                if not (ok_move(a, s2, s1) and ok_move(b, s1, s2)):
                    continue
                da = with_day(with_day(days[a], d1, -1), d2, +1)
                db = with_day(with_day(days[b], d2, -1), d1, +1)
                delta = (fcost(a, n[a], da) - fcost(a, n[a], days[a])) + (fcost(b, n[b], db) - fcost(b, n[b], days[b]))
                for (f, ss, add) in ((a, s1, -1), (b, s2, -1), (a, s2, 1), (b, s1, 1)):
                    if (f, ss) in problem.previous:
                        delta += add * w.repeat_previous
                    if (f, ss) in problem.keep_preferred:
                        delta -= add * w.change_from_baseline
                if -delta > best[0] + settings.comparable_cost_epsilon // 2:
                    best = (-delta, (s2, b))
            if best[1] is not None:
                s2, b = best[1]
                d2 = slot_by[s2].day_pos
                assign[s1].remove(a); assign[s1].append(b)
                assign[s2].remove(b); assign[s2].append(a)
                days[a][d1] -= 1; days[a][d2] += 1
                days[b][d2] -= 1; days[b][d1] += 1
                mine[a].discard(s1); mine[a].add(s2)
                mine[b].discard(s2); mine[b].add(s1)
                flat = [(s, f) for s, fids in assign.items() for f in fids if (f, s) not in fixed]
                improved = True
        seats = [(s, f) for s, fids in assign.items() for f in fids if (f, s) not in fixed]
    return assign

# ---------------------------------------------------------------------------
# one solve
# ---------------------------------------------------------------------------

def _solve_once(problem: Problem, settings: ExamSettings, targets: Dict[int, int],
                seed: int, hint: Optional[Dict[int, List[int]]] = None) -> Tuple[Optional[Dict[int, List[int]]], str]:
    w: OptimizerWeights = settings.weights
    scale = settings.target_scale
    rng = random.Random(seed)
    m = cp_model.CpModel()

    slots = {s.index: s for s in problem.slots}
    x: Dict[Tuple[int, int], cp_model.IntVar] = {}
    by_slot: Dict[int, List[int]] = defaultdict(list)
    by_fac: Dict[int, List[int]] = defaultdict(list)
    for f in problem.faculty:
        for s in sorted(problem.eligible.get(f.id, ())):
            if (f.id, s) in problem.forbidden:
                continue
            x[(f.id, s)] = m.NewBoolVar(f"x_{f.id}_{s}")
            by_slot[s].append(f.id)
            by_fac[f.id].append(s)
    for key in problem.fixed:
        if key in x:
            m.Add(x[key] == 1)

    cost: List[Any] = []

    # --- seats: never over-filled; empty seats are penalised heavily ------
    unfilled_total = []
    for s, sl in slots.items():
        got = sum(x[(f, s)] for f in by_slot[s]) if by_slot[s] else 0
        if by_slot[s]:
            m.Add(got <= sl.required)
        u = m.NewIntVar(0, sl.required, f"u_{s}")
        m.Add(u == sl.required - got)
        unfilled_total.append(u)
    cost.append(w.unfilled_slot * sum(unfilled_total))

    # --- group split: strong soft goal per slot and group --------------------
    if problem.groups:
        for s, quota in problem.group_quota.items():
            for g, q in quota.items():
                members = [f for f in by_slot.get(s, []) if problem.groups.get(f) == g]
                got_g = sum(x[(f, s)] for f in members) if members else 0
                dev = m.NewIntVar(0, max(q, len(members)) + 1, f"gd_{s}_{g}")
                m.Add(dev >= got_g - q)
                m.Add(dev >= q - got_g)
                cost.append(w.group_balance * dev)

    # --- overlapping slots -------------------------------------------------
    pairs = problem.overlaps or overlapping_pairs(problem.slots)
    for a, b in pairs:
        for f in by_slot[a]:
            if (f, b) in x:
                m.Add(x[(f, a)] + x[(f, b)] <= 1)

    days = sorted({s.day_pos for s in problem.slots})
    slots_of_day: Dict[int, List[int]] = defaultdict(list)
    for s in problem.slots:
        slots_of_day[s.day_pos].append(s.index)

    day_cap = problem.max_per_day or settings.max_shifts_per_day
    for f in problem.faculty:
        mine = by_fac.get(f.id, [])
        if not mine:
            continue
        n = sum(x[(f.id, s)] for s in mine)
        if problem.max_duties is not None:
            m.Add(n <= problem.max_duties)
        if day_cap:
            for d in days:
                row = [x[(f.id, s)] for s in slots_of_day[d] if (f.id, s) in x]
                if len(row) > day_cap:
                    m.Add(sum(row) <= day_cap)

        # --- distance to the fair target (convex => spreads duties) ------
        tgt = targets.get(f.id, 0)
        top = len(mine) * scale
        diff = m.NewIntVar(-max(tgt, 0) - top, top + abs(tgt), f"diff_{f.id}")
        m.Add(diff == n * scale - tgt)
        sq = m.NewIntVar(0, (top + abs(tgt)) ** 2 + 1, f"sq_{f.id}")
        m.AddMultiplicationEquality(sq, [diff, diff])
        cost.append(coef(w.target_deviation, scale) * sq)
        if f.tier > 1:
            cost.append((f.tier - 1) * w.tier_penalty * n)

        over = m.NewIntVar(0, top + abs(tgt), f"over_{f.id}")
        m.Add(over >= diff - scale)
        cost.append(coef(w.concentration, scale) * over)

        if problem.min_duties:
            need = min(problem.min_duties, len(mine))
            sh = m.NewIntVar(0, need, f"short_{f.id}")
            m.Add(sh >= need - n)
            cost.append(w.min_shortfall * sh)

        # --- day presence, consecutive days, rest, same-day repeats ------
        pres: Dict[int, cp_model.IntVar] = {}
        for d in days:
            row = [x[(f.id, s)] for s in slots_of_day[d] if (f.id, s) in x]
            if not row:
                continue
            p = m.NewBoolVar(f"p_{f.id}_{d}")
            for v in row:
                m.Add(p >= v)
            pres[d] = p
            if len(row) > 1:
                rep = m.NewIntVar(0, len(row) - 1, f"rep_{f.id}_{d}")
                m.Add(rep >= sum(row) - 1)
                cost.append(w.same_day_repeat * rep)
        dl = sorted(pres)
        pref = settings.rest_days_preferred
        for i, d1 in enumerate(dl):
            for d2 in dl[i + 1:]:
                g = d2 - d1
                if g == 1:
                    if problem.prohibit_consecutive:
                        m.Add(pres[d1] + pres[d2] <= 1)
                    y = m.NewBoolVar(f"c_{f.id}_{d1}_{d2}")
                    m.Add(y >= pres[d1] + pres[d2] - 1)
                    cost.append(w.consecutive_day * y)
                elif g - 1 < pref:
                    y = m.NewBoolVar(f"r_{f.id}_{d1}_{d2}")
                    m.Add(y >= pres[d1] + pres[d2] - 1)
                    cost.append(w.insufficient_rest * (pref - (g - 1)) * y)
                else:
                    break

    # --- change / variation / tie-breaking randomness ---------------------
    for key in problem.keep_preferred:
        if key in x and key not in problem.fixed:
            cost.append(w.change_from_baseline * (1 - x[key]))
    for key in problem.previous:
        if key in x:
            cost.append(w.repeat_previous * x[key])
    if w.jitter > 0:
        for key in sorted(x):
            j = rng.randint(0, w.jitter)
            if j:
                cost.append(j * x[key])

    m.Minimize(sum(cost))
    if hint is not None:
        chosen = {(f, sidx) for sidx, fids in hint.items() for f in fids}
        for key, var in x.items():
            m.AddHint(var, 1 if key in chosen else 0)

    solver = cp_model.CpSolver()
    p = solver.parameters
    p.max_time_in_seconds = settings.solver_max_seconds
    p.max_deterministic_time = settings.solver_deterministic_time
    p.num_workers = max(1, settings.solver_workers)
    p.random_seed = seed % (2 ** 31 - 1)
    p.log_search_progress = False
    status = solver.Solve(m)
    name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, name
    assign: Dict[int, List[int]] = {s: [] for s in slots}
    for (f, s), var in x.items():
        if solver.Value(var):
            assign[s].append(f)
    return assign, name


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def _unfilled_records(problem: Problem, assign: Dict[int, List[int]],
                      seats: Dict[int, List[Tuple[str, int, int]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    counts: Dict[int, int] = defaultdict(int)
    for fids in assign.values():
        for f in fids:
            counts[f] += 1
    overlap_of: Dict[int, Set[int]] = defaultdict(set)
    for a, b in (problem.overlaps or overlapping_pairs(problem.slots)):
        overlap_of[a].add(b)
        overlap_of[b].add(a)
    for sl in problem.slots:
        got = {(r, st) for r, st, _ in seats.get(sl.index, [])}
        missing = [(r, st) for r in sl.rooms for st in range(1, sl.per_room + 1) if (r, st) not in got]
        if not missing:
            continue
        elig = [f.id for f in problem.faculty if sl.index in problem.eligible.get(f.id, ())
                and (f.id, sl.index) not in problem.forbidden]
        here = set(assign.get(sl.index, []))
        spare = [f for f in elig if f not in here]
        at_max = [f for f in spare if problem.max_duties is not None and counts[f] >= problem.max_duties]
        clash = [f for f in spare if f not in at_max and
                 any(f in assign.get(o, ()) for o in overlap_of[sl.index])]
        rest = [f for f in spare if f not in at_max and f not in clash]
        if len(elig) < sl.required:
            why = (f"Only {len(elig)} faculty are eligible for this slot but {sl.required} are required "
                   f"(the others are excluded, unavailable or already committed).")
        else:
            bits = []
            if at_max:
                bits.append(f"{len(at_max)} eligible faculty already reached the maximum duty limit")
            if clash:
                bits.append(f"{len(clash)} are on duty in an overlapping slot")
            if rest:
                bits.append(f"{len(rest)} were left out by a rule (consecutive-duty / per-day limit)")
            why = (f"{len(elig)} eligible but {sl.required} required: " + "; ".join(bits) + ".")
        for r, st in missing:
            out.append({"exam_date": sl.exam_date.isoformat(), "shift_id": sl.shift_id, "room": r,
                        "seat": st, "required": sl.required, "available": len(elig), "reason": why,
                        "slot": sl.index})
    return out


def solve(problem: Problem, settings: Optional[ExamSettings] = None) -> SolveResult:
    """
    Generate the best of `settings.candidate_runs` controlled-random solutions.
    The returned allocation always satisfies every hard constraint; empty seats
    are listed in `unfilled` and make the status 'partial' (or 'infeasible').
    """

    settings = settings or ExamSettings()
    scale = settings.target_scale
    targets = targets_for(problem, settings)
    runs: List[Tuple[int, Dict[int, List[int]], str, Dict[str, Any]]] = []
    last_status = ""
    # 1. several controlled-random constructive solutions, compared by quality
    for k in range(max(1, settings.candidate_runs)):
        seed = problem.seed + k * 7919
        try:
            g = greedy_construct(problem, settings, targets, seed)
        except Exception:          # the CP-SAT model below is the safety net
            continue
        runs.append((seed, g, "CONSTRUCTIVE", evaluate(problem, g, targets, settings)))
    # polish the best constructive solutions with swaps before CP-SAT
    runs.sort(key=lambda r: (r[3]["score"], r[0]))
    polished = []
    for seed, g, _, _ in runs[:2]:
        g2 = local_search(problem, settings, targets, g, seed, settings.local_search_seconds)
        polished.append((seed, g2, "LOCAL_SEARCH", evaluate(problem, g2, targets, settings)))
    runs += polished
    # 2. CP-SAT polishes the best one (it can only keep or improve on the hint)
    hint_run = min(runs, key=lambda r: (r[3]["score"], r[0])) if runs else None
    seed0 = hint_run[0] if hint_run else problem.seed
    assign, st = _solve_once(problem, settings, targets, seed0, hint_run[1] if hint_run else None)
    last_status = st
    if assign is not None:
        runs.append((seed0, assign, st, evaluate(problem, assign, targets, settings)))
    if not runs:
        return SolveResult("infeasible", {}, {}, [], {"score": None}, problem.seed, last_status,
                           ["The constraints cannot be satisfied together (solver: %s)." % last_status])

    best = min(runs, key=lambda r: (r[3]["score"], r[0]))
    seed, assign, st, quality = best
    dept = {f.id: (problem.groups.get(f.id) or f.department) if problem.groups else f.department
            for f in problem.faculty}
    rng = random.Random(seed ^ 0x5EED)
    seats = {s.index: assign_rooms(s, assign.get(s.index, []), dept, rng) for s in problem.slots}
    unfilled = _unfilled_records(problem, assign, seats)
    counts: Dict[int, int] = defaultdict(int)
    for fids in assign.values():
        for f in fids:
            counts[f] += 1
    slot_days = {s.index: s.day_pos for s in problem.slots}
    quality = dict(quality)
    quality["fairness"] = fairness_metrics(problem.faculty, problem.eligible, counts, targets,
                                           slot_days, assign, scale)
    if problem.groups:
        gstats: Dict[str, Dict[str, Any]] = {}
        for g in sorted(set(problem.groups.values())):
            want = sum(q.get(g, 0) for q in problem.group_quota.values())
            got = sum(1 for fids in assign.values() for f in fids if problem.groups.get(f) == g)
            people = [f for f in problem.faculty if problem.groups.get(f.id) == g]
            gstats[g] = {"seats_wanted": want, "seats_filled": got, "faculty": len(people),
                         "eligible": sum(1 for f in people if problem.eligible.get(f.id))}
        quality["groups"] = gstats
    quality["runs"] = [{"seed": r[0], "score": r[3]["score"]} for r in runs]
    quality["targets"] = {str(k): v / scale for k, v in targets.items()}
    assigned_any = any(assign.values())
    if not unfilled:
        status = "complete"
    elif assigned_any:
        status = "partial"
    else:
        status = "infeasible"
    notes = []
    if problem.min_duties:
        short = [f.name for f in problem.faculty if problem.eligible.get(f.id)
                 and counts[f.id] < min(problem.min_duties, len(problem.eligible[f.id]))]
        quality["below_minimum"] = short
    return SolveResult(status, assign, seats, unfilled, quality, seed, st, notes)