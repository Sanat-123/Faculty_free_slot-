"""
============================================================
UNISCHED AI - UNIVERSAL FREE SLOT CHATBOT
============================================================

User uploads one or more timetable files:
    PDF
    XLSX
    XLS
    CSV

The application:
    1. Imports every uploaded file
    2. Detects each file's timetable structure
    3. Combines all imported records into one dataset
    4. Creates canonical records from the combined dataset
    5. Builds ONE availability engine over that dataset
    6. Hands that SAME canonical dataset to a FacultyAIChatbot
       instance (scheduling/workload/absence/lab-shift/
       what-if/exam-duty), without importing the uploaded
       files a second time
    7. Accepts natural-language questions
    8. Returns timetable/free-slot answers, or, once the
       richer engine is ready, absence/replacement,
       multi-absence, lab-shifting, what-if, and exam-duty
       answers too - proposals always wait for an explicit
       follow-up "confirm" before anything is persisted.

============================================================
"""

from __future__ import annotations

import os
import sys
import shutil
import tempfile
import hashlib
import time
from pathlib import Path

import streamlit as st


# ============================================================
# PROJECT ROOT
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(
        0,
        str(PROJECT_ROOT)
    )


# ============================================================
# PROJECT IMPORTS
# ============================================================

from import_engine.import_manager import ImportManager

from data_engine.canonical_event_matcher import (
    CanonicalEventMatcher
)

from query_engine import (
    QueryEngine,
    NaturalLanguageQuery
)

from faculty_chatbot import FacultyAIChatbot


# ============================================================
# PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="UniSched AI",
    page_icon="🎓",
    layout="wide"
)


# ============================================================
# TITLE
# ============================================================

st.title("🎓 UniSched AI")

st.subheader(
    "AI-Based Faculty & Classroom Free Slot Detection System"
)

st.write(
    "Upload one or more timetable PDF, Excel or CSV files "
    "and ask questions in natural language."
)


# ============================================================
# SUPPORTED FILE TYPES
# ============================================================

SUPPORTED_TYPES = [
    "pdf",
    "xlsx",
    "xls",
    "csv"
]


# ============================================================
# SESSION STATE
#
# IMPORTANT: every mutable piece of the uploaded knowledge base
# lives in st.session_state, which Streamlit keeps separate per
# browser session. Nothing here is stored in a module-level
# variable, a global dict, or an @st.cache_resource-style shared
# cache - that would let one user's uploaded timetable leak into
# another user's session.
# ============================================================

if "records" not in st.session_state:
    st.session_state.records = []

if "matcher" not in st.session_state:
    st.session_state.matcher = None

if "engine" not in st.session_state:
    st.session_state.engine = None

if "nlp" not in st.session_state:
    st.session_state.nlp = None

if "chatbot" not in st.session_state:
    # The richer FacultyAIChatbot instance (workload, absence,
    # multi-absence, lab-shifting, what-if, exam duty), built
    # from the SAME canonical matcher as st.session_state.matcher
    # above - see build_combined_dataset(). None until a
    # dataset has been loaded and the engine has finished
    # constructing successfully.
    st.session_state.chatbot = None

if "chatbot_error" not in st.session_state:
    # Set if FacultyAIChatbot construction fails, so the UI can
    # explain why only read-only timetable queries are available
    # (via st.session_state.nlp) instead of the full engine.
    st.session_state.chatbot_error = None

if "file_names" not in st.session_state:
    st.session_state.file_names = []

if "upload_fingerprint" not in st.session_state:
    # Deterministic content-based fingerprint (filename + bytes,
    # order-invariant) of the LAST uploaded file set that was
    # actually processed - see compute_upload_fingerprint()
    # below. While the current upload's fingerprint matches this,
    # nothing is re-imported/re-built; the existing session
    # knowledge base (or the existing error, if every file failed
    # last time) is reused as-is.
    st.session_state.upload_fingerprint = None

if "build_duration_seconds" not in st.session_state:
    # Real elapsed wall-clock time (time.perf_counter()) that the
    # last successful build actually took - shown to the user
    # instead of a fake progress percentage.
    st.session_state.build_duration_seconds = None

if "file_reports" not in st.session_state:
    # Per-file status from the last build (filename, file type,
    # status, record count, message) - kept in session_state so
    # it stays visible on every rerun, not just the one right
    # after upload.
    st.session_state.file_reports = []

if "build_error" not in st.session_state:
    # Set when EVERY uploaded file produced zero records - no
    # chatbot/engine is built in that case, and this message
    # explains why.
    st.session_state.build_error = None

if "messages" not in st.session_state:
    st.session_state.messages = []


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header("📁 Timetable Upload")

    uploaded_files = st.file_uploader(
        "Upload timetable(s)",
        type=SUPPORTED_TYPES,
        accept_multiple_files=True
    )

    st.markdown("---")

    st.markdown(
        """
### Supported files

- PDF
- Excel (.xlsx)
- Excel (.xls)
- CSV

### Example questions

- Who is free on Monday slot 2?
- Which faculty is free on Monday slot 3?
- Is Dr. Mehul Mahrishi free on Monday slot 3?
- What is Dr. Mehul Mahrishi teaching on Monday?
- Who teaches OS III?
- What is the timetable of 3CS-D on Monday?
- Which room is free on Monday slot 4?

### Once the full engine is ready

- What is the workload of \\<faculty\\> on Monday?
- \\<Faculty\\> is absent on Monday, who can replace them?
- What if \\<faculty\\> is absent on Thursday?
- Shift the lab in room \\<room\\> to another room on \\<day\\>
- Suggest 2 faculty for exam duty on \\<date\\> from
  \\<time\\> to \\<time\\>

Any of these that produce a proposal will ask you to reply
"confirm" before anything is actually saved.
        """
    )


# ============================================================
# FILE PROCESSING FUNCTION
# ============================================================

def import_single_file(uploaded_file):

    """
    Save ONE uploaded Streamlit file temporarily and import it
    through the existing ImportManager.

    This function only performs the IMPORT step for a single
    file. It deliberately does NOT build a CanonicalEventMatcher
    or QueryEngine -- that happens once, after every uploaded
    file's records have been combined (see
    build_combined_dataset() below).

    IMPORTANT: the temporary file is written using the file's
    ORIGINAL name, not a randomly generated one. This keeps the
    *filename-based* fallback in CanonicalEventMatcher.
    identify_source() available for a file literally named
    "...facultywise...", "...classwise..." or "...location
    wise...". It is only a fallback now: PDFImporter itself
    detects each PAGE's identity (teacher / class / room) from
    the page's own CONTENT, independent of what the file is
    named - see import_engine/pdf_importer.py's
    detect_page_identity(). The filename is kept for backward
    compatibility and as a fallback for pages whose content-based
    identity could not be determined.

    Returns a dict with an explicit `status`:

        "success"       - at least one record was imported
        "zero_records"  - imported without error, but produced
                           no timetable records
        "failed"        - the file could not be imported at all
    """

    temp_dir = None

    temp_path = None

    try:

        # ----------------------------------------------------
        # Create a fresh temp directory and write the file
        # there under its ORIGINAL name.
        # ----------------------------------------------------

        temp_dir = tempfile.mkdtemp()

        temp_path = str(
            Path(
                temp_dir
            ) / uploaded_file.name
        )

        with open(
            temp_path,
            "wb"
        ) as temp_file:

            temp_file.write(
                uploaded_file.getbuffer()
            )


        # ----------------------------------------------------
        # Import
        # ----------------------------------------------------

        manager = ImportManager()

        result = manager.import_file(
            temp_path
        )


        # ----------------------------------------------------
        # Handle ImportManager result
        # ----------------------------------------------------

        if isinstance(result, dict):

            file_type = result.get(
                "file_type",
                ""
            )

            if not result.get(
                "success",
                False
            ):

                return {
                    "success": False,
                    "status": "failed",
                    "file_type": file_type,
                    "message": result.get(
                        "error",
                        "File import failed."
                    ),
                    "records": [],
                    "record_count": 0,
                    "warnings": []
                }

            records = result.get(
                "records",
                []
            )

            warnings = result.get(
                "warnings",
                []
            )

        else:

            file_type = ""

            records = result

            warnings = []


        # ----------------------------------------------------
        # Validate records
        # ----------------------------------------------------

        if not records:

            return {
                "success": False,
                "status": "zero_records",
                "file_type": file_type,
                "message": (
                    "The file was imported but "
                    "no timetable records were found."
                ),
                "records": [],
                "record_count": 0,
                "warnings": warnings
            }

        return {
            "success": True,
            "status": "success",
            "file_type": file_type,
            "records": records,
            "record_count": len(records),
            "warnings": warnings
        }


    except Exception as e:

        return {
            "success": False,
            "status": "failed",
            "file_type": "",
            "message": str(e),
            "records": [],
            "record_count": 0,
            "warnings": []
        }


    finally:

        # ----------------------------------------------------
        # Remove temporary directory (and the file in it)
        # ----------------------------------------------------

        if temp_dir:

            try:
                shutil.rmtree(
                    temp_dir,
                    ignore_errors=True
                )
            except Exception:
                pass


# ============================================================
# COMBINE MULTIPLE UPLOADED FILES INTO ONE DATASET
# ============================================================

def build_combined_dataset(uploaded_files):

    """
    Import EVERY uploaded file, combine all of their records
    into a single list, and build exactly ONE
    CanonicalEventMatcher / QueryEngine / NaturalLanguageQuery
    over that combined list.

    A single uploaded file is simply the special case of this
    same code path with one file in the list -- there is no
    separate single-file pipeline.
    """

    all_records = []

    all_warnings = []

    file_reports = []

    for uploaded_file in uploaded_files:

        file_result = import_single_file(
            uploaded_file
        )

        if file_result["success"]:

            all_records.extend(
                file_result["records"]
            )

            file_reports.append(
                {
                    "name": uploaded_file.name,
                    "file_type": file_result.get(
                        "file_type",
                        ""
                    ),
                    "status": "success",
                    "success": True,
                    "record_count": file_result.get(
                        "record_count",
                        len(file_result["records"])
                    ),
                    "message": "",
                }
            )

            for warning in file_result.get(
                "warnings",
                []
            ):

                all_warnings.append(
                    f"{uploaded_file.name}: {warning}"
                )

        else:

            file_reports.append(
                {
                    "name": uploaded_file.name,
                    "file_type": file_result.get(
                        "file_type",
                        ""
                    ),
                    "status": file_result.get(
                        "status",
                        "failed"
                    ),
                    "success": False,
                    "record_count": 0,
                    "message": file_result.get(
                        "message",
                        "Unknown error"
                    ),
                }
            )


        # ------------------------------------------------
        # No records from ANY uploaded file.
        # ------------------------------------------------

    if not all_records:

        return {
            "success": False,
            "message": (
                "None of the uploaded files produced any "
                "timetable records."
            ),
            "records": [],
            "file_reports": file_reports,
        }


    # ----------------------------------------------------
    # Canonical matching -- ONE matcher over ALL combined
    # records from every uploaded file.
    # ----------------------------------------------------

    matcher = CanonicalEventMatcher(
        all_records
    )

    matcher.match()


    # ----------------------------------------------------
    # Query engine -- ONE engine / ONE NLP layer over the
    # combined dataset.
    # ----------------------------------------------------

    engine = QueryEngine(
        matcher
    )

    nlp = NaturalLanguageQuery(
        engine
    )


    return {
        "success": True,
        "records": all_records,
        "matcher": matcher,
        "engine": engine,
        "nlp": nlp,
        "warnings": all_warnings,
        "file_reports": file_reports,
    }


# ============================================================
# PROPOSAL / CONFIRMATION RESPONSE STYLING
#
# FacultyAIChatbot.process_query() already follows PLAN ->
# VALIDATE -> CONFIRM internally (see scheduling/*_planner.py /
# *_coordinator.py) and returns a plain-text answer either way -
# it never persists anything on a plan/proposal request, only on
# an explicit follow-up "confirm". These two small helpers only
# affect how that same text is DISPLAYED, so a proposal awaiting
# confirmation and an already-confirmed/persisted change are
# visually easy to tell apart. They never change what gets
# persisted.
# ============================================================

def _classify_chatbot_response(text):

    if not text:
        return "info"

    first_line = text.strip().splitlines()[0].strip().lower()

    if first_line.startswith("confirmed"):
        return "confirmed"

    if (
        first_line.startswith("proposed")
        or '"confirm"' in text.lower()
    ):
        return "proposal"

    return "info"


def _render_assistant_message(text):

    kind = _classify_chatbot_response(text)

    if kind == "confirmed":

        st.success(text)

    elif kind == "proposal":

        st.info(text)

        st.caption(
            "⏳ This is a proposal only - nothing has been "
            "saved yet. Reply \"confirm\" to apply it."
        )

    else:

        st.markdown(text)


# ============================================================
# UPLOAD FINGERPRINT
#
# A deterministic, CONTENT-based fingerprint for the current set
# of uploaded files. This - not the filename list - is what
# decides whether the (expensive) import/canonicalization/engine-
# construction work needs to run again.
# ============================================================

def compute_upload_fingerprint(uploaded_files):

    """
    Hashes each file's normalized filename + actual bytes with
    SHA-256, then combines the per-file hashes in SORTED order.

    This means the fingerprint:
      - changes if any file's bytes change
      - changes if a file is added or removed
      - changes if a file is replaced by a different one under
        the same name
      - does NOT change merely because the same files were
        selected/uploaded in a different order
    """

    per_file_hashes = []

    for uploaded_file in uploaded_files:

        normalized_name = (
            uploaded_file.name
            .strip()
            .lower()
        )

        content = uploaded_file.getvalue()

        file_hash = hashlib.sha256()

        file_hash.update(
            normalized_name.encode("utf-8")
        )

        file_hash.update(
            b"\x00"
        )

        file_hash.update(
            content
        )

        per_file_hashes.append(
            file_hash.hexdigest()
        )

    # Order-invariant: sort so upload ORDER never affects the
    # final fingerprint, only the underlying set of files.
    per_file_hashes.sort()

    combined_hash = hashlib.sha256()

    for file_hash_hex in per_file_hashes:

        combined_hash.update(
            file_hash_hex.encode("utf-8")
        )

    return combined_hash.hexdigest()


# ============================================================
# PROCESS UPLOAD
# ============================================================

if uploaded_files:

    with st.spinner(
        "Checking uploaded files..."
    ):

        current_fingerprint = compute_upload_fingerprint(
            uploaded_files
        )

    fingerprint_unchanged = (
        st.session_state.upload_fingerprint
        == current_fingerprint
    )

    if fingerprint_unchanged and (
        st.session_state.nlp is not None
    ):

        # ------------------------------------------------------
        # SAME CONTENT AS LAST TIME - reuse the existing session
        # knowledge base. No import, no CanonicalEventMatcher
        # rebuild, no QueryEngine/NLP rebuild, no FacultyAIChatbot
        # rebuild.
        # ------------------------------------------------------

        duration = (
            st.session_state.build_duration_seconds
        )

        duration_text = (
            f"{duration:.2f}s"
            if duration is not None
            else "an earlier run"
        )

        st.info(
            "🔁 Reused existing timetable knowledge base "
            "(these exact files were already processed in "
            f"{duration_text})."
        )

    elif fingerprint_unchanged and (
        st.session_state.build_error
    ):

        # Same failing file set as last time - don't re-run the
        # same expensive, doomed import again.

        st.error(
            st.session_state.build_error
        )

    else:

        # ------------------------------------------------------
        # NEW/CHANGED CONTENT - rebuild exactly once.
        # ------------------------------------------------------

        current_file_names = [
            uploaded_file.name
            for uploaded_file in uploaded_files
        ]

        build_start = time.perf_counter()

        with st.spinner(
            f"Processing {len(uploaded_files)} "
            f"timetable file(s)..."
        ):

            result = build_combined_dataset(
                uploaded_files
            )

        build_duration = (
            time.perf_counter()
            - build_start
        )

        file_reports = result.get(
            "file_reports",
            []
        )

        successful_reports = [
            report
            for report in file_reports
            if report.get("status") == "success"
        ]

        needs_attention_reports = [
            report
            for report in file_reports
            if report.get("status") != "success"
        ]

        st.session_state.upload_fingerprint = (
            current_fingerprint
        )

        st.session_state.file_reports = (
            file_reports
        )

        st.session_state.build_duration_seconds = (
            build_duration
        )


        if successful_reports:

            # ------------------------------------------------
            # AT LEAST ONE FILE PRODUCED RECORDS - continue with
            # the valid dataset, clearly warning about any
            # zero-record/failed files.
            # ------------------------------------------------

            st.session_state.records = (
                result["records"]
            )

            st.session_state.matcher = (
                result["matcher"]
            )

            st.session_state.engine = (
                result["engine"]
            )

            st.session_state.nlp = (
                result["nlp"]
            )

            st.session_state.file_names = (
                current_file_names
            )

            st.session_state.build_error = None

            st.session_state.messages = []


            # --------------------------------------------------
            # FULL UNISCHED AI ENGINE
            #
            # Reuses the SAME CanonicalEventMatcher just built
            # above from the uploaded files - FacultyAIChatbot
            # is never given the raw uploaded files itself, so
            # ImportManager never runs a second time over them.
            # This is what exposes workload / absence /
            # multi-absence / lab-shifting / what-if / exam-duty
            # in the web UI, on top of the same canonical
            # dataset the read-only timetable queries already
            # use.
            # --------------------------------------------------

            try:

                st.session_state.chatbot = FacultyAIChatbot(
                    matcher=st.session_state.matcher
                )

                st.session_state.chatbot_error = None

            except Exception as e:

                # Read-only timetable queries
                # (st.session_state.nlp) still work even if the
                # richer engine fails to build - see the
                # ENGINE STATUS / chat sections below.
                st.session_state.chatbot = None

                st.session_state.chatbot_error = str(e)


            # --------------------------------------------------
            # TRUTHFUL top-line status
            # --------------------------------------------------

            total_count = len(file_reports)
            success_count = len(successful_reports)
            attention_count = len(needs_attention_reports)

            if attention_count == 0:

                st.success(
                    f"{total_count} file(s) uploaded — all "
                    f"processed successfully in "
                    f"{build_duration:.2f}s."
                )

            else:

                st.warning(
                    f"{total_count} file(s) uploaded — "
                    f"{success_count} processed successfully — "
                    f"{attention_count} need attention (see "
                    f"file details below). Continuing with the "
                    f"{success_count} valid file(s). Built in "
                    f"{build_duration:.2f}s."
                )


            # --------------------------------------------------
            # Warnings
            # --------------------------------------------------

            warnings = result.get(
                "warnings",
                []
            )

            if warnings:

                with st.expander(
                    "⚠️ Import warnings"
                ):

                    for warning in warnings:

                        st.warning(
                            warning
                        )


        else:

            # ------------------------------------------------
            # ALL FILES RETURNED ZERO RECORDS - do not build a
            # fake/empty chatbot or engine. Show a clear error.
            # ------------------------------------------------

            st.session_state.records = []

            st.session_state.matcher = None

            st.session_state.engine = None

            st.session_state.nlp = None

            st.session_state.chatbot = None

            st.session_state.chatbot_error = None

            st.session_state.file_names = []

            st.session_state.messages = []

            error_message = (
                f"{len(current_file_names)} file(s) uploaded — "
                f"0 produced any timetable records. See file "
                f"details below."
            )

            st.session_state.build_error = (
                error_message
            )

            st.error(
                error_message
            )


# ============================================================
# DATASET STATUS
#
# Always rendered from st.session_state (not just right after
# upload), so per-file status and dataset stats stay visible on
# every rerun - including while chatting, when the fingerprint
# is unchanged and nothing was rebuilt.
# ============================================================

if st.session_state.file_reports:

    st.markdown("---")

    total_count = len(
        st.session_state.file_reports
    )

    success_count = sum(
        1
        for report in st.session_state.file_reports
        if report.get("status") == "success"
    )

    attention_count = (
        total_count
        - success_count
    )

    expander_title = (
        f"📄 File details ({total_count} uploaded — "
        f"{success_count} processed successfully"
    )

    if attention_count:

        expander_title += (
            f" — {attention_count} need attention)"
        )

    else:

        expander_title += ")"

    with st.expander(
        expander_title,
        expanded=bool(attention_count)
    ):

        for report in st.session_state.file_reports:

            file_type_text = (
                report.get("file_type")
                or "unknown"
            ).upper()

            status = report.get(
                "status",
                "failed"
            )

            if status == "success":

                st.write(
                    f"✅ **{report['name']}** "
                    f"({file_type_text}) — "
                    f"{report['record_count']} "
                    f"record(s) imported"
                )

            elif status == "zero_records":

                st.write(
                    f"⚠️ **{report['name']}** "
                    f"({file_type_text}) — "
                    f"0 records — "
                    f"{report.get('message', '')}"
                )

            else:

                st.write(
                    f"❌ **{report['name']}** "
                    f"({file_type_text}) — "
                    f"{report.get('message', 'Unknown error')}"
                )


if st.session_state.nlp:

    st.markdown("---")

    st.subheader(
        "📊 Dataset Information"
    )

    records = (
        st.session_state.records
    )

    matcher = (
        st.session_state.matcher
    )

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:

        st.metric(
            "Files",
            len(
                st.session_state.file_names
            )
        )

    with col2:

        st.metric(
            "Imported Records",
            len(
                records
            )
        )

    with col3:

        st.metric(
            "Canonical Events",
            len(
                matcher.events
            )
        )

    with col4:

        st.metric(
            "Faculty Free Slots",
            len(
                matcher.faculty_free_slots
            )
        )

    with col5:

        st.metric(
            "Room Free Slots",
            len(
                matcher.room_free_slots
            )
        )

    if (
        len(matcher.room_free_slots) == 0
        and len(matcher.faculty_free_slots) == 0
    ):

        st.info(
            "The uploaded data does not contain enough "
            "information to infer free slots. Timetable search "
            "and other questions may still work."
        )

    with st.expander(
        f"**Files ({len(st.session_state.file_names)}):**"
    ):

        for file_name in st.session_state.file_names:

            st.write(
                f"- {file_name}"
            )


# ============================================================
# ENGINE STATUS
#
# Small, simple status area (no dashboard/redesign) showing
# whether the full UNISCHED AI engine (FacultyAIChatbot -
# workload, absence, multi-absence, lab-shifting, what-if,
# exam duty) is ready, in addition to the always-available
# read-only timetable queries above.
# ============================================================

if st.session_state.nlp:

    st.markdown("---")

    st.subheader(
        "🧠 UniSched AI Engine Status"
    )

    if st.session_state.chatbot is not None:

        st.success(
            "Full engine ready - workload, absence/"
            "replacement, multi-absence, lab-shifting, "
            "what-if, and exam-duty requests are all "
            "available in the chat below, in addition to "
            "read-only timetable questions."
        )

    elif st.session_state.chatbot_error:

        st.warning(
            "The full scheduling engine could not be "
            "started, so only read-only timetable questions "
            "are available below.\n\n"
            f"Error: `{st.session_state.chatbot_error}`"
        )

    else:

        st.info(
            "Only read-only timetable questions are "
            "available below."
        )


# ============================================================
# CHATBOT
# ============================================================

if st.session_state.nlp:

    st.markdown("---")

    st.header(
        "💬 Ask UniSched AI"
    )


    # --------------------------------------------------------
    # Display previous messages
    # --------------------------------------------------------

    for message in st.session_state.messages:

        with st.chat_message(
            message["role"]
        ):

            if message["role"] == "assistant":

                _render_assistant_message(
                    message["content"]
                )

            else:

                st.markdown(
                    message["content"]
                )


    # --------------------------------------------------------
    # Chat input
    # --------------------------------------------------------

    prompt = st.chat_input(
        "Ask a timetable question..."
    )


    if prompt:

        # ----------------------------------------------------
        # Display user message
        # ----------------------------------------------------

        st.session_state.messages.append(
            {
                "role": "user",
                "content": prompt
            }
        )


        with st.chat_message(
            "user"
        ):

            st.markdown(
                prompt
            )


        # ----------------------------------------------------
        # Generate answer
        #
        # Prefer the full FacultyAIChatbot (workload, absence,
        # multi-absence, lab-shifting, what-if, exam duty, and
        # read-only timetable queries all in one place) when it
        # is ready. Fall back to the read-only
        # NaturalLanguageQuery layer only if the full engine
        # could not be built for this dataset, so read-only
        # timetable questions keep working either way.
        # ----------------------------------------------------

        with st.chat_message(
            "assistant"
        ):

            with st.spinner(
                "Analyzing timetable..."
            ):

                try:

                    if st.session_state.chatbot is not None:

                        answer = (
                            st.session_state
                            .chatbot
                            .process_query(prompt)
                        )

                    else:

                        answer = (
                            st.session_state
                            .nlp
                            .answer(prompt)
                        )

                except Exception as e:

                    answer = (
                        "I could not process "
                        "that question.\n\n"
                        f"Error: `{e}`"
                    )


            _render_assistant_message(
                answer
            )


        # ----------------------------------------------------
        # Save answer
        # ----------------------------------------------------

        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": answer
            }
        )


# ============================================================
# NO FILE MESSAGE
# ============================================================

else:

    st.info(
        "👈 Upload one or more timetable files from the "
        "sidebar to start chatting."
    )


# ============================================================
# FOOTER
# ============================================================

st.markdown("---")

st.caption(
    "UniSched AI | AI-Based Faculty & Classroom "
    "Free Slot Detection System"
)