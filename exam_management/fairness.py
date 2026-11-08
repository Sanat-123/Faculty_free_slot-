"""
Fairness: workload targets and measurable statistics.

TARGET (how many NEW duties each eligible person "should" get)

  1. capacity_f  = min(slots the person is genuinely eligible for, max duties)
  2. water-filling: raise a common "combined workload" level L until
         sum_f clamp(L - (historical_f + committed_f), 0, capacity_f) = demand
     -> people with fewer past duties get more now, people with many get fewer,
        nobody gets more than they can actually take.
  3. opportunity share: demand is also split in proportion to eligible slots
     (someone free for 3 days is not expected to carry as much as someone free
     for 10).
  4. target = (1 - blend) * water-fill + blend * opportunity share.

Historical duty count is therefore one input among several - never the only one -
and exclusions are applied BEFORE this (capacity comes from eligibility).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .models import FacultyInfo, SlotInfo


def _waterfill(base: Mapping[int, float], cap: Mapping[int, float], demand: float) -> Dict[int, float]:
    ids = [i for i in base if cap[i] > 0]
    if not ids or demand <= 0:
        return {i: 0.0 for i in base}
    lo = min(base[i] for i in ids)
    hi = max(base[i] + cap[i] for i in ids)
    for _ in range(80):
        mid = (lo + hi) / 2
        got = sum(min(max(mid - base[i], 0.0), cap[i]) for i in ids)
        if got < demand:
            lo = mid
        else:
            hi = mid
    level = hi
    out = {i: 0.0 for i in base}
    for i in ids:
        out[i] = min(max(level - base[i], 0.0), cap[i])
    return out


def _proportional(weight: Mapping[int, float], cap: Mapping[int, float], demand: float) -> Dict[int, float]:
    out = {i: 0.0 for i in weight}
    open_ids = [i for i in weight if cap[i] > 0 and weight[i] > 0]
    remaining = demand
    for _ in range(30):
        if not open_ids or remaining <= 1e-9:
            break
        total_w = sum(weight[i] for i in open_ids)
        capped = []
        for i in open_ids:
            share = remaining * weight[i] / total_w
            if out[i] + share >= cap[i] - 1e-9:
                capped.append(i)
        if not capped:
            for i in open_ids:
                out[i] += remaining * weight[i] / total_w
            remaining = 0
            break
        for i in capped:
            remaining -= cap[i] - out[i]
            out[i] = cap[i]
            open_ids.remove(i)
    return out


def compute_targets(faculty: Sequence[FacultyInfo], eligible: Mapping[int, Set[int]],
                    demand: int, max_duties: Optional[int], blend: float,
                    scale: int = 10, min_duties: Optional[int] = None) -> Dict[int, int]:
    """
    Fixed-point targets (value * scale) of NEW duties per faculty id.

    The minimum (when given and when demand can cover it for everyone) is a FLOOR:
    those duties are pre-assigned in the target and only the rest is shared by the
    water-fill / opportunity rules.
    """

    cap: Dict[int, float] = {}
    base: Dict[int, float] = {}
    for f in faculty:
        n = len(eligible.get(f.id, ()))
        cap[f.id] = float(min(n, max_duties) if max_duties is not None else n)
        base[f.id] = f.history + f.committed
    capacity = sum(cap.values())
    d = float(min(demand, capacity))
    floor = {i: 0.0 for i in base}
    if min_duties:
        want = {i: float(min(min_duties, cap[i])) for i in base}
        if sum(want.values()) <= d:
            floor = want
    rest = d - sum(floor.values())
    cap2 = {i: cap[i] - floor[i] for i in base}
    base2 = {i: base[i] + floor[i] for i in base}
    wf = _waterfill(base2, cap2, rest)
    op = _proportional({i: float(len(eligible.get(i, ()))) for i in base}, cap2, rest)
    blend = min(max(blend, 0.0), 1.0)
    return {i: int(round((floor[i] + (1 - blend) * wf[i] + blend * op[i]) * scale)) for i in base}


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def _dist(values: Sequence[float]) -> Dict[str, float]:
    if not values:
        return {"min": 0, "max": 0, "avg": 0.0, "std": 0.0}
    return {"min": min(values), "max": max(values), "avg": round(sum(values) / len(values), 2),
            "std": round(statistics.pstdev(values), 2)}


def day_positions(duties: Iterable[Tuple[int, int, int]]) -> Dict[int, List[int]]:
    """(fid, day_pos, slot) -> fid -> sorted distinct day positions."""

    d: Dict[int, Set[int]] = defaultdict(set)
    for fid, pos, _ in duties:
        d[fid].add(pos)
    return {k: sorted(v) for k, v in d.items()}


def consecutive_stats(days: Mapping[int, Sequence[int]]) -> Dict[str, Any]:
    pairs = 0
    who = []
    gaps: List[int] = []
    for fid, pos in days.items():
        n = 0
        for a, b in zip(pos, pos[1:]):
            gaps.append(b - a)
            if b - a == 1:
                n += 1
        if n:
            who.append((fid, n))
        pairs += n
    return {"consecutive_pairs": pairs, "faculty_with_consecutive": who,
            "min_gap": min(gaps) if gaps else None,
            "avg_gap": round(sum(gaps) / len(gaps), 2) if gaps else None}


def fairness_metrics(faculty: Sequence[FacultyInfo], eligible: Mapping[int, Set[int]],
                     new_counts: Mapping[int, int], targets: Mapping[int, int],
                     slot_days: Mapping[int, int], assignments: Mapping[int, Sequence[int]],
                     scale: int = 10) -> Dict[str, Any]:
    """All figures come from the actual assignment - nothing is estimated."""

    pool = [f for f in faculty if eligible.get(f.id)]
    new = [new_counts.get(f.id, 0) for f in pool]
    hist = [f.history for f in pool]
    comb = [f.history + f.committed + new_counts.get(f.id, 0) for f in pool]
    dev = [abs(new_counts.get(f.id, 0) - targets.get(f.id, 0) / scale) for f in pool]
    duties = [(fid, slot_days[s], s) for s, fids in assignments.items() for fid in fids]
    days = day_positions(duties)
    cons = consecutive_stats(days)
    same_day = 0
    per_day: Dict[Tuple[int, int], int] = defaultdict(int)
    for fid, pos, _ in duties:
        per_day[(fid, pos)] += 1
    same_day = sum(1 for v in per_day.values() if v > 1)
    return {
        "eligible_faculty": len(pool),
        "avg_new": round(sum(new) / len(new), 2) if new else 0,
        "min_new": min(new) if new else 0,
        "max_new": max(new) if new else 0,
        "historical": _dist(hist),
        "current": _dist(new),
        "combined": _dist(comb),
        "workload_std": _dist(comb)["std"],
        "mean_abs_target_deviation": round(sum(dev) / len(dev), 3) if dev else 0.0,
        "assigned_faculty": sum(1 for f in faculty if new_counts.get(f.id, 0) > 0),
        "zero_duty_eligible": sum(1 for f in pool if new_counts.get(f.id, 0) == 0),
        "same_day_repeats": same_day,
        **cons,
    }