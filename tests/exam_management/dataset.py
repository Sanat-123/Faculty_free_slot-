"""
Realistic TEST data (never used by the application).  Departments, names, counts
are generated here only so tests can run; production data comes from the timetable.
"""

from __future__ import annotations

import itertools
import random
from datetime import date, timedelta
from typing import Dict, List, Tuple

FIRST = ["Aarav", "Meera", "Rohit", "Sunita", "Vikram", "Anjali", "Karan", "Priya", "Nitin", "Kavita",
         "Manoj", "Deepa", "Suresh", "Rekha", "Amit", "Neha", "Rajesh", "Pooja", "Arun", "Swati"]
LAST = ["Sharma", "Verma", "Gupta", "Mehta", "Singh", "Joshi", "Rao", "Nair", "Iyer", "Das",
        "Bose", "Khan", "Patel", "Reddy", "Jain", "Mishra", "Kulkarni", "Menon", "Saxena", "Bhatt"]
DEPTS = ["Computer Science", "Mechanical", "Civil", "Electrical", "Electronics", "Management"]


def roster(per_dept: int = 10, depts: List[str] = None, seed: int = 7) -> List[Tuple[str, str]]:
    """Unique 'Dr./Prof. First Last' names spread over departments."""

    depts = depts or DEPTS
    rng = random.Random(seed)
    combos = list(itertools.product(FIRST, LAST))
    rng.shuffle(combos)
    out, it = [], iter(combos)
    for d in depts:
        for i in range(per_dept):
            f, l = next(it)
            out.append((f"{'Dr.' if i % 3 == 0 else 'Prof.'} {f} {l}", d))
    return out


def history(rows: List[Tuple[str, str]], seed: int = 3, lo: int = 0, hi: int = 10) -> Dict[str, float]:
    rng = random.Random(seed)
    return {n: float(rng.randint(lo, hi)) for n, _ in rows}


def days(start: date, n: int) -> List[date]:
    return [start + timedelta(days=i) for i in range(n)]


SHIFTS_ONE = [{"label": "Morning", "start": "09:00", "end": "12:00"}]
SHIFTS_TWO = [{"label": "Morning", "start": "09:00", "end": "12:00"},
              {"label": "Afternoon", "start": "14:00", "end": "17:00"}]