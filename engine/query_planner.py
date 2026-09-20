class QueryPlanner:

    """
    Connects the NLP chatbot with the existing QueryEngine.
    """

    _default_engine = None

    @classmethod
    def default_query_engine(cls):
        """
        The QueryEngine of the project's default dataset, built lazily
        once.  Used only when plan() is called without an explicit
        query_engine (older scripts/tests call it with three arguments).
        """

        if cls._default_engine is None:

            import contextlib
            import io

            from faculty_chatbot import FacultyAIChatbot

            with contextlib.redirect_stdout(io.StringIO()):
                cls._default_engine = FacultyAIChatbot().query_engine

        return cls._default_engine

    @staticmethod
    def plan(
        intent,
        entities,
        day_slot,
        query_engine=None
    ):

        if query_engine is None:
            query_engine = QueryPlanner.default_query_engine()

        # ==================================================
        # EXTRACT ENTITIES
        # ==================================================

        teachers = entities.get("teachers", [])
        subjects = entities.get("subjects", [])
        rooms = entities.get("rooms", [])
        classes = entities.get("classes", [])

        teacher = (
            teachers[0]["value"]
            if teachers
            else None
        )

        subject = (
            subjects[0]["value"]
            if subjects
            else None
        )

        room = (
            rooms[0]["value"]
            if rooms
            else None
        )

        class_name = (
            classes[0]["value"]
            if classes
            else None
        )

        day = day_slot.get("day")
        slot = day_slot.get("slot")
        time_range = day_slot.get("time_range")

        # ==================================================
        # FACULTY STATUS
        #
        # Example:
        # Is Mr. Nitin free on Monday slot 2?
        # Is Mr. Nitin busy on Monday slot 2?
        # ==================================================

        if intent == "FACULTY_STATUS":

            if not teacher:

                return {
                    "count": 0,
                    "results": [],
                    "message": (
                        "Please specify a faculty member."
                    )
                }

            if not day:

                return {
                    "count": 0,
                    "results": [],
                    "message": (
                        "Please specify a day."
                    )
                }

            if slot is None:

                return {
                    "count": 0,
                    "results": [],
                    "message": (
                        "Please specify a slot number."
                    )
                }

            return query_engine.faculty_status(
                teacher=teacher,
                day=day,
                slot=slot
            )

        # ==================================================
        # FIND TEACHER
        # ==================================================

        if intent == "FIND_TEACHER":

            if not subject:

                return {
                    "count": 0,
                    "results": [],
                    "message": (
                        "Please specify a subject."
                    )
                }

            result = query_engine.subject_search(
                subject
            )

            results = result.get(
                "results",
                []
            )

            # Filter by day
            if day:

                results = [
                    r for r in results
                    if str(
                        r.get("day", "")
                    ).lower() == day.lower()
                ]

            # Filter by slot
            if slot is not None:

                results = [
                    r for r in results
                    if query_engine._slot(
                        r.get("slot")
                    ) == query_engine._slot(slot)
                ]

            return {
                "count": len(results),
                "results": results
            }

        # ==================================================
        # FIND CLASS TEACHER
        #
        # Example:
        # Who teaches 7CS?
        # Show faculty teaching 7CS
        # Who teaches 7CS on Monday?
        #
        # Dynamically reuses the existing
        # query_engine.class_schedule() lookup - the same
        # method already used by SHOW_TIMETABLE for a class.
        # No class name, faculty name, or day is hard-coded.
        #
        # CLASS RESOLUTION:
        # Uses query_engine.resolve_class_reference() - the
        # SAME resolver SHOW_TIMETABLE's class branch below
        # uses - so "7CS" is interpreted identically by both
        # query types. An EXACT canonical class (e.g.
        # "7CS-DS") narrows to just that class; a BROAD query
        # (e.g. "7CS", a prefix shared by several canonical
        # classes) covers every matching class and says so.
        # ==================================================

        if intent == "FIND_CLASS_TEACHER":

            if not class_name:

                return {
                    "count": 0,
                    "results": [],
                    "message": (
                        "Please specify a class."
                    )
                }

            raw_class_text = (
                classes[0].get("text")
                if classes and isinstance(classes[0], dict)
                else None
            )

            resolution = query_engine.resolve_class_reference(
                raw_class_text or class_name
            )

            if resolution["mode"] == "exact":
                lookup_class_name = resolution["class_name"]
                display_class_name = resolution["class_name"]
            else:
                lookup_class_name = raw_class_text or class_name
                display_class_name = (
                    (raw_class_text or class_name).upper()
                )

            result = query_engine.class_schedule(
                class_name=lookup_class_name,
                day=day,
                slot=slot
            )

            results = result.get(
                "results",
                []
            )

            return {
                "count": len(results),
                "results": results,
                "class_name": display_class_name,
                "class_query_mode": resolution["mode"],
                "matching_classes": resolution["matching_classes"],
                "day": result.get("day", day)
            }

        # ==================================================
        # FIND SUBJECT
        # ==================================================

        if intent == "FIND_SUBJECT":

            if not teacher:

                return {
                    "count": 0,
                    "results": [],
                    "message": (
                        "Please specify a faculty member."
                    )
                }

            result = query_engine.teacher_search(
                teacher
            )

            results = result.get(
                "results",
                []
            )

            # Filter by day
            if day:

                results = [
                    r for r in results
                    if str(
                        r.get("day", "")
                    ).lower() == day.lower()
                ]

            # Filter by slot
            if slot is not None:

                results = [
                    r for r in results
                    if query_engine._slot(
                        r.get("slot")
                    ) == query_engine._slot(slot)
                ]

            return {
                "count": len(results),
                "results": results
            }

        # ==================================================
        # SHOW TIMETABLE
        # ==================================================

        if intent == "SHOW_TIMETABLE":

            # ------------------------------
            # Teacher timetable
            # ------------------------------

            if teacher:

                return query_engine.teacher_schedule(
                    teacher=teacher,
                    day=day,
                    slot=slot
                )

            # ------------------------------
            # Class timetable
            #
            # Uses the SAME query_engine.resolve_class_reference()
            # resolver as FIND_CLASS_TEACHER above, so "7CS" is
            # interpreted identically whether the user asks "who
            # teaches 7CS" or "show timetable of 7CS".
            # ------------------------------

            if class_name:

                raw_class_text = (
                    classes[0].get("text")
                    if classes and isinstance(classes[0], dict)
                    else None
                )

                resolution = (
                    query_engine.resolve_class_reference(
                        raw_class_text or class_name
                    )
                )

                if resolution["mode"] == "exact":
                    lookup_class_name = resolution["class_name"]
                    display_class_name = resolution["class_name"]
                else:
                    lookup_class_name = (
                        raw_class_text or class_name
                    )
                    display_class_name = (
                        (raw_class_text or class_name).upper()
                    )

                result = query_engine.class_schedule(
                    class_name=lookup_class_name,
                    day=day,
                    slot=slot
                )

                result["class_name"] = display_class_name
                result["class_query_mode"] = resolution["mode"]
                result["matching_classes"] = (
                    resolution["matching_classes"]
                )

                return result

            # ------------------------------
            # Room timetable
            # ------------------------------

            if room:

                return query_engine.room_schedule(
                    room=room,
                    day=day,
                    slot=slot
                )

            # ------------------------------
            # Subject timetable
            # ------------------------------

            if subject:

                result = query_engine.subject_search(
                    subject
                )

                results = result.get(
                    "results",
                    []
                )

                if day:

                    results = [
                        r for r in results
                        if str(
                            r.get("day", "")
                        ).lower() == day.lower()
                    ]

                if slot is not None:

                    results = [
                        r for r in results
                        if query_engine._slot(
                            r.get("slot")
                        ) == query_engine._slot(slot)
                    ]

                return {
                    "count": len(results),
                    "results": results
                }

            return {
                "count": 0,
                "results": [],
                "message": (
                    "Please specify a faculty, "
                    "class, room, or subject."
                )
            }

        # ==================================================
        # FIND ROOM
        # ==================================================

        if intent == "FIND_ROOM":

            if not subject:

                return {
                    "count": 0,
                    "results": [],
                    "message": (
                        "Please specify a subject."
                    )
                }

            result = query_engine.subject_search(
                subject
            )

            results = result.get(
                "results",
                []
            )

            if day:

                results = [
                    r for r in results
                    if str(
                        r.get("day", "")
                    ).lower() == day.lower()
                ]

            if slot is not None:

                results = [
                    r for r in results
                    if query_engine._slot(
                        r.get("slot")
                    ) == query_engine._slot(slot)
                ]

            return {
                "count": len(results),
                "results": results
            }

        # ==================================================
        # FIND FREE FACULTY
        # ==================================================

        if intent == "FIND_FREE_FACULTY":

            # A day is required
            if not day:

                return {
                    "count": 0,
                    "results": [],
                    "message": "Please specify a day."
                }

            # ==================================================
            # TIME RANGE
            # ==================================================

            if time_range:

                start_time = time_range[0]
                end_time = time_range[1]

                result = query_engine.faculty_free_for_period(
                    day=day,
                    start_time=start_time,
                    end_time=end_time,
                    teacher=teacher
                )

                result["time_range"] = time_range

                return result

            # ==================================================
            # SPECIFIC SLOT
            # ==================================================

            if slot is not None:

                result = query_engine.faculty_free_slots(
                    day=day,
                    slot=slot,
                    teacher=teacher
                )

                return result

            # ==================================================
            # ENTIRE DAY
            # ==================================================

            return query_engine.faculty_free_slots(
                day=day,
                slot=None,
                teacher=teacher
            )

        # ==================================================
        # UNKNOWN INTENT
        # ==================================================

        return {
            "count": 0,
            "results": [],
            "message": (
                "I could not understand the query."
            )
        }