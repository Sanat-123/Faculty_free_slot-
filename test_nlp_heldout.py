"""
Held-out sentences: written in different styles, NOT used to tune the parser.

They are here to catch regressions in how an exam-duty request is READ (dates, times,
students, room size, invigilators per room, duty-count rule, reserves) when the wording
is new.  If a real coordinator sentence is read wrongly, add it here, fix the wording
lists in config/duty_lexicon.json (or the reader), and keep this file passing.

Usage:  python test_nlp_heldout.py
"""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scheduling import exam_duty_requests as er, semester_exam as se
T=datetime.date(2026,10,28); D=datetime.date
CL=["3CSA","5CSA","5CSB","7CSA"]
H=[
# (text, expected subset)
("5th semester paper is on 12 nov, 9:00 am to 11:00 am, 600 students sit 40 in each room, need 2 invigilators in every room, teachers must have under 70 duties",
 dict(students=600,capacity=40,per_room=2,limit=("<",70.0),dates=[D(2026,11,12)],times=[("09:00","11:00")])),
("Please allot duty for 3rd sem examination dated 8/11/2026 timing 10 to 12 noon. strength 360, each hall 30 students, one teacher per hall",
 dict(students=360,capacity=30,per_room=1,dates=[D(2026,11,8)])),
("exam of sem 7 on 20th november in two shifts 9-10:30 and 2-3:30; total 450 students; 45 students per class; 2 teachers per class; duty count < 90",
 dict(students=450,capacity=45,per_room=2,limit=("<",90.0),times=[("09:00","10:30"),("14:00","15:30")])),
("i want to take 5th sem exams on 3rd november between 8 and 9:30 am and 1 and 2:30 pm. there are around 520 students. a room can accomodate 40. keep 3 spare teachers",
 dict(students=520,capacity=40,reserves=3,times=[("08:00","09:30"),("13:00","14:30")])),
("duty chart needed for 5 sem exam 2/11 at 9 to 10:30, 480 candidates, 30 candidates in one room, 2 faculty each room, only those with duties less than 60, no repeat",
 dict(students=480,capacity=30,per_room=2,limit=("<",60.0),distinct_per_day=True)),
("3rd semester exam on 4 november 1pm to 3pm. 300 students, class capacity is 25, put 2 teachers in each class, no more than 55 duties, 10% reserve",
 dict(students=300,capacity=25,per_room=2,limit=("<=",55.0),reserve_pct=10)),
("exam is scheduled for 7th semester on 9 november 10 am - 12 pm.  we have 250 students and every room holds 25 so assign two invigilators per room from teachers having duty count lower than 40",
 dict(students=250,capacity=25,per_room=2,limit=("<",40.0))),
("sem 5 test on 6 nov (9 to 10:30) and (1 to 2:30), 560 students, 40 students will sit in one classroom, 2 teachers per classroom, whose duties are less than 100, absent replacements 2",
 dict(students=560,capacity=40,per_room=2,limit=("<",100.0),times=[("09:00","10:30"),("13:00","14:30")])),
("assign exam duty 5th sem 10th november 8 to 9:30, teachers with less than 100 duties. students 500, 50 in each room. 2 teachers in 1 room. if any teacher is absent there should be 2 replacement",
 dict(students=500,capacity=50,per_room=2,limit=("<",100.0),reserves=2)),
("we need invigilators for 3rd sem final exam on 15 nov at 9am-12pm for 270 students. each room fits 30. use teachers who have done fewer than 20 duties",
 dict(students=270,capacity=30,limit=("<",20.0),times=[("09:00","12:00")])),
("exam duty for semester five on 16th november 9 to 10:30, one room has 35 students total 700 students, two invigilators each room, keep 4 extra teachers as backup",
 dict(students=700,capacity=35,per_room=2,reserves=4)),
("there is exam of 5th sem on 5th november in timing 9 to 10:30 and and 1 to 2:30 assign exam duties to the teachers who are having duty count less then 80 and the same teacher should not be present in both the slots of exam duty assign classrooms also for this time period there are 500 students and 25 students in each class and 2 faculties must be assigned in each class",
 dict(students=500,capacity=25,rooms_needed=20,per_room=2,total=None,limit=("<",80.0),distinct_per_day=True,times=[("09:00","10:30"),("13:00","14:30")])),
("there is exam of 5th sem on 5th november so assign exam duty to the teachers whose total duty count is less then 100 and there are 500 students and in each class there will be 50 students allot exam duties from timing 8 to 9:30 and 1 to 2:30",
 dict(students=500,capacity=50,rooms_needed=10,limit=("<",100.0),times=[("08:00","09:30"),("13:00","14:30")])),
("exam 5 nov 9 to 12 assign 2 faculties to each class, 100 students, 25 in each class", dict(students=100,capacity=25,per_room=2,total=None)),
("exam 5 nov 9 to 12 each class must have 2 faculties, 100 students, 25 per class", dict(students=100,capacity=25,per_room=2)),
("exam 5 nov 9 to 12 2 invigilators must be appointed for each room, 100 students and 25 per room", dict(students=100,capacity=25,per_room=2)),
("exam 5 nov 9 to 12 allot two teachers for every classroom, 100 students, 25 in each classroom", dict(students=100,capacity=25,per_room=2,total=None)),
("exam 5 nov 9 to 12 keep 3 teachers in each hall, 100 students, room capacity 25", dict(students=100,capacity=25,per_room=3)),
("exam 5 nov 9 to 12, 100 students, 25 in each class, 2 faculties", dict(students=100,capacity=25,per_room=2,total=None,per_room_assumed=True)),
]
bad=0; tot=0
for t,exp in H:
    c=er.clean_text(t); r=er.parse_request(c,T); sp=se.find_scope(c,CL)
    got={k:getattr(r,k) for k in exp}
    miss={k:(v,got[k]) for k,v in exp.items() if got[k]!=v}
    tot+=len(exp)
    if miss:
        bad+=len(miss); print("MISS",miss,"\n   >",t[:150])
print(f"{tot-bad}/{tot} held-out checks passed")
sys.exit(1 if bad else 0)