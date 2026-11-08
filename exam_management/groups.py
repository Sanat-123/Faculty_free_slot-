"""
Group split of examination duties (e.g. CS / non-CS, or any two or more groups).

A split is DATA, stored on the session (options["group_split"]):

    {"groups": {"CS": ["CS"], "Non-CS": ["*"]},      # group -> department labels; "*" = everyone else
     "shares": {"CS": 0.5, "Non-CS": 0.5}}           # share of the seats of every slot

Nothing here knows any department name: labels come from the coordinator and the roster.
Each slot's seats are divided by share (largest-remainder rounding); the optimiser then
treats the quota as a strong soft goal (shortages are reported, never faked) and computes
fairness targets inside each group, so a small group is not overloaded unfairly.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def resolve_group(department: str, spec: Dict[str, Any]) -> Optional[str]:
    """Group of a department label ("*" is the catch-all); None if the split has no home for it."""

    dept = (department or "").strip().lower()
    fallback = None
    for name, labels in spec["groups"].items():
        low = [str(x).strip().lower() for x in labels]
        if dept and dept in low:
            return name
        if "*" in low:
            fallback = name
    return fallback


def group_map(faculty: Iterable[Tuple[int, str]], spec: Optional[Dict[str, Any]]) -> Dict[int, str]:
    if not spec:
        return {}
    out: Dict[int, str] = {}
    for fid, dept in faculty:
        g = resolve_group(dept, spec)
        if g is not None:
            out[fid] = g
    return out


def seat_quota(required: int, shares: Dict[str, float]) -> Dict[str, int]:
    """Largest-remainder split of `required` seats by share (sums exactly to `required`)."""

    total = sum(max(v, 0.0) for v in shares.values()) or 1.0
    raw = {g: required * max(v, 0.0) / total for g, v in shares.items()}
    base = {g: int(x) for g, x in raw.items()}
    left = required - sum(base.values())
    for g, _ in sorted(raw.items(), key=lambda kv: (-(kv[1] - int(kv[1])), kv[0]))[:left]:
        base[g] += 1
    return base


def normalise_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    groups = {str(k): [str(x) for x in v] for k, v in spec["groups"].items()}
    shares = {str(k): float(v) for k, v in (spec.get("shares") or {}).items()}
    for g in groups:
        shares.setdefault(g, 1.0)
    if sum(shares.values()) <= 0:
        shares = {g: 1.0 for g in groups}
    return {"groups": groups, "shares": shares}


def describe(spec: Dict[str, Any]) -> str:
    total = sum(spec["shares"].values()) or 1.0
    parts = []
    for g, labels in spec["groups"].items():
        what = "all other departments" if "*" in labels else ", ".join(labels)
        parts.append(f"{g} ({what}): {spec['shares'][g] / total:.0%}")
    return "; ".join(parts)