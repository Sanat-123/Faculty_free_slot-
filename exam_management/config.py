"""
Central configuration.

Nothing in the allocation logic embeds a number, weight or policy: it reads it
from an ExamSettings object.  Defaults live ONLY here.  They can be overridden

  1. per process, by a JSON file named in EXAM_MGMT_SETTINGS_FILE,
  2. per college, by rows in the `exm_settings` table (key -> JSON value),
  3. per request, by the coordinator (natural language) - stored on the session.

Business rules here are *configurable defaults*, not hard-coded behaviour.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class OptimizerWeights:
    """Weights of the objective function (all integers; higher = worse)."""

    unfilled_slot: int = 100000        # one invigilator seat left empty
    target_deviation: int = 40         # (duties - fair target)^2, in tenths^2 / 10
    min_shortfall: int = 5000          # per duty below the minimum (when feasible)
    consecutive_day: int = 300         # duties on two consecutive exam days
    insufficient_rest: int = 120       # per missing rest day below the preferred gap
    same_day_repeat: int = 200         # a second shift on the same day
    concentration: int = 15            # duties beyond a person's share of the period
    change_from_baseline: int = 400    # moving an assignment that could be kept
    repeat_previous: int = 25          # re-using a pairing of the previous version
    jitter: int = 6                    # max random cost per (person, slot) - tie breaker
    group_balance: int = 1500          # per seat away from a group's share of a slot
    department_mix: int = 4            # two invigilators of one department in a room


@dataclass(frozen=True)
class ExamSettings:
    # ---- storage -------------------------------------------------------
    db_filename: str = "exam_management.db"

    # ---- optimisation --------------------------------------------------
    weights: OptimizerWeights = field(default_factory=OptimizerWeights)
    solver_max_seconds: float = 8.0       # wall-clock safety limit
    solver_deterministic_time: float = 4.0  # reproducible limit (solver "ticks")
    solver_workers: int = 1                # 1 => reproducible with a seed
    candidate_runs: int = 8                # constructive solutions compared per generation
    comparable_cost_epsilon: int = 6       # costs within this are "comparable"
    variation_quality_tolerance: float = 0.10   # allowed quality loss for "another"

    # ---- fairness ------------------------------------------------------
    opportunity_blend: float = 0.30        # 0 = only equalise combined load,
                                           # 1 = only share by eligible slots
    local_search_seconds: float = 1.5      # swap-improvement budget per constructive run
    rest_days_preferred: int = 1           # exam days of rest wanted between duties
    max_shifts_per_day: Optional[int] = None   # hard cap per day (None = soft only)
    target_scale: int = 10                 # fixed-point scale of fractional targets

    # ---- calendar ------------------------------------------------------
    non_working_weekdays: Tuple[str, ...] = ("sunday",)  # skipped when a range is expanded
    default_year_window_days: int = 183    # dates further in the past roll to next year

    # ---- policy ----------------------------------------------------------
    require_class_suspension_answer: bool = True   # ask, never assume
    large_operation_threshold: int = 5     # bulk ops above this need confirmation
    actor: str = "coordinator"
    # words that mark a role/duty as administrative (lexicon, not a list of people or roles)
    admin_keywords: Tuple[str, ...] = ("hod", "head of department", "dean", "coordinator", "director",
                                       "principal", "registrar", "controller", "naac", "nba",
                                       "accreditation", "committee", "administrat", "admin")
    leave_keywords: Tuple[str, ...] = ("leave", "sick", "medical", "vacation", "travel", "out of station",
                                       "away", "conference")

    def with_overrides(self, **kw: Any) -> "ExamSettings":
        weights = kw.pop("weights", None)
        if isinstance(weights, dict):
            weights = replace(self.weights, **{k: v for k, v in weights.items()
                                               if k in {f.name for f in fields(OptimizerWeights)}})
        known = {f.name for f in fields(ExamSettings)}
        clean = {k: v for k, v in kw.items() if k in known}
        if weights is not None:
            clean["weights"] = weights
        if "non_working_weekdays" in clean:
            clean["non_working_weekdays"] = tuple(str(d).lower() for d in clean["non_working_weekdays"])
        return replace(self, **clean)


def _from_mapping(base: ExamSettings, data: Dict[str, Any]) -> ExamSettings:
    return base.with_overrides(**data)


def load_settings(conn=None, base: Optional[ExamSettings] = None) -> ExamSettings:
    """Defaults -> settings file -> `exm_settings` table."""

    s = base or ExamSettings()
    path = os.environ.get("EXAM_MGMT_SETTINGS_FILE")
    if path and Path(path).is_file():
        try:
            s = _from_mapping(s, json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    if conn is not None:
        try:
            rows = conn.execute("SELECT key, value FROM exm_settings").fetchall()
            data = {}
            for r in rows:
                try:
                    data[r[0]] = json.loads(r[1])
                except ValueError:
                    continue
            s = _from_mapping(s, data)
        except Exception:
            pass
    return s