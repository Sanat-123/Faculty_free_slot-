DATABASE_NAME = "faculty.db"

PROJECT_NAME = "Faculty Free Slot AI"

DEFAULT_SEARCH_MODE = "partial"


# Exam-duty buffer: for "who is free from 9 to 12" a teacher must be free
# for the range PLUS this many minutes before and after it.
# (0 = exact range.  Can also be set with the environment variable
#  FACULTY_RANGE_BUFFER_MINUTES, or per question: "... no buffer".)
import os as _os

FREE_RANGE_BUFFER_MINUTES = int(
    _os.environ.get("FACULTY_RANGE_BUFFER_MINUTES", "15")
)
