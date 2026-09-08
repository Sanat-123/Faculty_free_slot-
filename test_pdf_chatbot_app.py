"""
End-to-end test for the PDF chatbot app using Streamlit's AppTest.

Run standalone:
    python test_pdf_chatbot_app.py

Run under pytest:
    python -m pytest test_pdf_chatbot_app.py -q
    python -m pytest -q          (collects it along with everything else)

All of the actual scenario logic lives in main(), which RETURNS an
exit code (0 = all checks passed, 1 = at least one failed) instead
of raising SystemExit itself. This is what makes the file safe for
pytest collection: pytest imports every test_*.py module it
discovers, and a bare `raise SystemExit(...)` sitting at module
level (rather than behind `if __name__ == "__main__":`) would
raise SystemExit during that import/collection step itself, which
pytest cannot recover from. Two thin wrappers call main():

    - `if __name__ == "__main__": sys.exit(main())` for standalone
      command-line use, which still exits with a non-zero status
      on failure exactly as before.
    - `test_pdf_chatbot_app_scenarios()`, a normal pytest test
      function (pytest discovers it by its `test_` prefix) that
      calls main() and asserts its return code is 0. Because this
      only runs when pytest actually EXECUTES the test - never at
      import/collection time - it cannot cause a collection error.
"""

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest


def main() -> int:
    """
    Runs the full AppTest-based scenario suite against
    pdf_chatbot_app.py and returns a process-style exit code
    (0 = all checks passed, 1 = at least one failed). Never
    calls sys.exit()/raises SystemExit itself, so it is safe
    to call from a pytest test function as well as from the
    __main__ guard below - see test_pdf_chatbot_app_scenarios().
    """

    SAMPLE = Path("data") / "Facultywise TT 20 sep.pdf"

    PASS = 0
    FAIL = 0

    def check(label, condition, extra=""):
        nonlocal PASS, FAIL
        if condition:
            PASS += 1
            print(f"  ✅ {label}")
        else:
            FAIL += 1
            print(f"  ❌ {label} {extra}")


    # ---------------------------------------------------------------
    # 1. App boots and shows the empty state
    # ---------------------------------------------------------------

    print("\n[1] App boot + empty state")

    at = AppTest.from_file("pdf_chatbot_app.py", default_timeout=120)
    at.run()

    check("no exceptions on first run", not at.exception,
          f"-> {at.exception}")
    check("sample button present",
          any("Load sample PDF" in b.label for b in at.button))

    # ---------------------------------------------------------------
    # 2. Load the sample PDF through the sidebar button
    # ---------------------------------------------------------------

    print("\n[2] Load sample PDF")

    sample_button = next(b for b in at.button if "Load sample PDF" in b.label)
    sample_button.click().run()

    check("no exceptions after loading PDF", not at.exception,
          f"-> {at.exception}")
    check("PDF name stored in session",
          at.session_state["pdf_name"] == SAMPLE.name,
          f"-> {at.session_state['pdf_name']}")

    stats = at.session_state["stats"]
    check("83 teachers parsed", stats["teachers"] == 83,
          f"-> {stats['teachers']}")
    check("702 timetable records", stats["records"] == 702,
          f"-> {stats['records']}")
    check("stats metrics shown",
          any(m.value == "83" for m in at.metric),
          f"-> {[m.value for m in at.metric]}")

    # ---------------------------------------------------------------
    # 3. Ask about free faculty
    # ---------------------------------------------------------------

    print("\n[3] Chat: free faculty query")

    at.chat_input[0].set_value("Who is free on Monday slot 3?").run()

    check("no exceptions after chat", not at.exception,
          f"-> {at.exception}")

    assistant_text = " ".join(
        m.value for m in at.markdown
        if m.value and "Available Faculty" in str(m.value)
    )

    check("answer mentions available faculty",
          "Available Faculty" in assistant_text,
          f"-> {assistant_text[:100]}")

    check("detection expander shows FIND_FREE_FACULTY",
          any("FIND_FREE_FACULTY" in str(m.value) for m in at.markdown),
          "-> detection not rendered")

    # ---------------------------------------------------------------
    # 4. Ask a timetable question (table view)
    # ---------------------------------------------------------------

    print("\n[4] Chat: timetable query (table)")

    at.chat_input[0].set_value("Show timetable of 3CS-DS-A").run()

    check("no exceptions", not at.exception, f"-> {at.exception}")

    check("dataframe rendered for timetable",
          len(at.dataframe) >= 1,
          f"-> {len(at.dataframe)} dataframes")

    if at.dataframe:
        check("timetable table has rows",
              len(at.dataframe[0].value) > 0,
              f"-> {len(at.dataframe[0].value)} rows")

    check("detection shows SHOW_TIMETABLE",
          any("SHOW_TIMETABLE" in str(m.value) for m in at.markdown))

    # ---------------------------------------------------------------
    # 5. Free-room bonus intent
    # ---------------------------------------------------------------

    print("\n[5] Chat: free rooms query")

    at.chat_input[0].set_value("Which rooms are free on Tuesday slot 4?").run()

    check("no exceptions", not at.exception, f"-> {at.exception}")

    check("answer mentions free rooms",
          any("Free rooms on Tuesday slot 4" in str(m.value) for m in at.markdown),
          "-> free rooms answer missing")

    check("detection shows FIND_FREE_ROOM",
          any("FIND_FREE_ROOM" in str(m.value) for m in at.markdown))

    # ---------------------------------------------------------------
    # 6. Upload path (file uploader with real PDF bytes)
    # ---------------------------------------------------------------

    print("\n[6] Upload PDF via file uploader")

    at2 = AppTest.from_file("pdf_chatbot_app.py", default_timeout=120)
    at2.run()

    check("file uploader present", len(at2.file_uploader) >= 1)

    at2.file_uploader[0].set_value(
        [("uploaded_timetable.pdf", SAMPLE.read_bytes(), "application/pdf")]
    ).run()

    check("no exceptions after upload", not at2.exception,
          f"-> {at2.exception}")
    check("uploaded name stored",
          at2.session_state["pdf_name"] == "uploaded_timetable.pdf",
          f"-> {at2.session_state['pdf_name']}")

    at2.chat_input[0].set_value("Who teaches Python?").run()

    check("upload path answers queries",
          any("Teacher" in str(m.value) and "Archika" in str(m.value)
              for m in at2.markdown),
          "-> answer missing")

    # ---------------------------------------------------------------
    # 7. Invalid PDF shows a friendly error (no crash)
    # ---------------------------------------------------------------

    print("\n[7] Invalid PDF upload")

    at3 = AppTest.from_file("pdf_chatbot_app.py", default_timeout=120)
    at3.run()

    at3.file_uploader[0].set_value(
        [("broken.pdf", b"this is not a real pdf file", "application/pdf")]
    ).run()

    check("no exceptions with broken PDF", not at3.exception,
          f"-> {at3.exception}")

    check("friendly error is shown",
          any("Could not load the PDF" in str(e.value) for e in at3.error),
          "-> error element missing")

    check("no PDF activated after failure",
          at3.session_state["pdf_name"] is None,
          f"-> {at3.session_state['pdf_name']}")

    # ---------------------------------------------------------------

    print(f"\n{'=' * 50}")
    print(f"PASSED: {PASS}   FAILED: {FAIL}")
    print(f"{'=' * 50}")

    return 1 if FAIL else 0


def test_pdf_chatbot_app_scenarios():
    """
    Pytest entry point. Runs the exact same AppTest-based
    scenario suite as `python test_pdf_chatbot_app.py` (via
    main()) and asserts every check passed. main() never
    raises SystemExit, so this behaves as a normal pytest
    test rather than aborting the test process.
    """

    exit_code = main()

    assert exit_code == 0, "one or more pdf_chatbot_app.py AppTest checks failed "\
        "(see printed output above for details)"


if __name__ == "__main__":
    sys.exit(main())