"""
Many human ways of saying the same exam-duty request must read the same.

Typos ("faculities", "1: to 2:30", "less then 50"), odd clock notations, number
words, "12 rooms x 2", "50 or fewer duties", "nobody repeated" ... Nothing here is
a fixed sentence: the cleaner (engine/text_normalizer.py) and the wording lists
(config/duty_lexicon.json) have to cope with all of them.

Usage:  python test_nlp_phrasings.py
"""
import datetime, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduling import exam_duty_requests as er
from engine.text_normalizer import normalize

T = datetime.date(2026, 10, 28)

# (text, expected dict of fields)
C=[
# --- times
("exam on 30 november time 9:00 to 10:30 and 1: to 2:30", dict(times=[("09:00","10:30"),("13:00","14:30")])),
("30 nov 9am-10:30am and 1pm-2:30pm", dict(times=[("09:00","10:30"),("13:00","14:30")])),
("30 nov 9.00 to 10.30 & 1.00 to 2.30", dict(times=[("09:00","10:30"),("13:00","14:30")])),
("30 nov morning slot 9 till 10:30 afternoon slot 1 till 2:30", dict(times=[("09:00","10:30"),("13:00","14:30")])),
("30 nov between 9 and 10:30, and between 1 and 2:30", dict(times=[("09:00","10:30"),("13:00","14:30")])),
("30 nov 0900-1030 and 1300-1430", dict(times=[("09:00","10:30"),("13:00","14:30")])),
("30 nov from 9:00 a.m. to 10:30 a.m. and from 1:00 p.m. to 2:30 p.m.", dict(times=[("09:00","10:30"),("13:00","14:30")])),
("30 nov first shift 9 to 10:30 second shift 1 to 2:30", dict(times=[("09:00","10:30"),("13:00","14:30")])),
("30 nov 9 to 10:30 / 1 to 2:30", dict(times=[("09:00","10:30"),("13:00","14:30")])),
# --- quantities
("30 nov 9 to 12 i want faculties in 12 rooms in both slot and 2 faculties in each room", dict(room_count=12, per_room=2)),
("30 nov 9 to 12 12 rooms, 2 invigilators each", dict(room_count=12, per_room=2)),
("30 nov 9 to 12 need 2 teachers in each of the 12 rooms", dict(room_count=12, per_room=2)),
("30 nov 9 to 12 twelve rooms with two invigilators in every room", dict(room_count=12, per_room=2)),
("30 nov 9 to 12 12 rooms x 2 faculty", dict(room_count=12, per_room=2)),
("30 nov 9 to 12 2 faculty per room across 12 rooms", dict(room_count=12, per_room=2)),
("30 nov 9 to 12 24 invigilators", dict(total=24)),
("30 nov 9 to 12 i need 24 faculities", dict(total=24)),
("30 nov 9 to 12 12 rooms two teachrs each", dict(room_count=12, per_room=2)),
("30 nov 9 to 12 faculty for 8 halls, 3 per hall", dict(room_count=8, per_room=3)),
# --- limits
("assign on 30 nov 9 to 12 6 faculty having duties less then 50", dict(limit=("<",50.0))),
("assign on 30 nov 9 to 12 6 faculty with 50 or fewer duties", dict(limit=("<=",50.0))),
("assign on 30 nov 9 to 12 6 faculty whose duty count is not exceeding 50", dict(limit=("<=",50.0))),
("assign on 30 nov 9 to 12 6 faculty who have done under 50 duties", dict(limit=("<",50.0))),
("assign on 30 nov 9 to 12 6 faculty duties < 50", dict(limit=("<",50.0))),
("assign on 30 nov 9 to 12 6 faculty having lesser than 50 exam duties allotted", dict(limit=("<",50.0))),
("assign on 30 nov 9 to 12 6 faculty having 49 or less duties", dict(limit=("<=",49.0))),
("assign on 30 nov 9 to 12 6 faculty with duties upto 40", dict(limit=("<=",40.0))),
# --- distinct
("30 nov 9 to 10:30 and 1 to 2:30 nobody should be repeated in both slots", dict(distinct_per_day=True)),
("30 nov 9 to 10:30 and 1 to 2:30 a teacher must not get duty in both slots", dict(distinct_per_day=True)),
("30 nov 9 to 10:30 and 1 to 2:30 different faculty for each slot", dict(distinct_per_day=True)),
("30 nov 9 to 10:30 and 1 to 2:30 same teacher shouldn't be in both", dict(distinct_per_day=True)),
("30 nov 9 to 10:30 and 1 to 2:30 no repetition of teachers", dict(distinct_per_day=True)),
# --- dates
("exam on 30th october", dict(dates=[datetime.date(2026,10,30)])),
("exam on oct 30 9 to 12", dict(dates=[datetime.date(2026,10,30)])),
("exam on 30/10/2026 9 to 12", dict(dates=[datetime.date(2026,10,30)])),
("exam on 30-10-2026 9 to 12", dict(dates=[datetime.date(2026,10,30)])),
("exam on 30 octobar 9 to 12", dict(dates=[datetime.date(2026,10,30)])),
("exam on 30 noveber 9 to 12", dict(dates=[datetime.date(2026,11,30)])),
("exam on 2 and 3 november 9 to 12", dict(dates=[datetime.date(2026,11,2),datetime.date(2026,11,3)])),
("exam from 2 nov to 4 nov 9 to 12", dict(dates=[datetime.date(2026,11,2),datetime.date(2026,11,3),datetime.date(2026,11,4)])),
]

bad = 0

for text, exp in C:
    r = er.understand(text, T)
    got = {k: getattr(r, k) for k in exp}
    if got != exp:
        bad += 1
        print("FAIL:", text, "\n   want", exp, "\n   got ", got)

# words that merely LOOK like vocabulary words must be left alone
for text in ["clear confirmed exam duties", "she teaches on monday", "Dr. Kajal Mathur is teaching"]:
    if normalize(text, er.duty_history.lexicon()["spell_vocabulary"], {"kajal", "mathur"}) != text:
        bad += 1
        print("FAIL: changed a correct sentence:", text)

total = len(C) + 3
print(f"{total - bad}/{total} phrasing checks passed")
sys.exit(1 if bad else 0)