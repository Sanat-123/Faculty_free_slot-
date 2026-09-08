import sys

from import_engine.pdf_importer import PDFImporter


def main() -> int:
    """
    Runs the PDF importer inspection/extraction test and
    returns a process-style exit code. Never calls sys.exit()/
    raises SystemExit itself, so it is safe to call from a
    pytest test function as well as from the __main__ guard
    below - see test_pdf_importer_extraction().

    NOTE: preserves the original script's exact exit-code
    behavior, including the bare `raise SystemExit` (which
    exits with status 0, not 1) when file validation fails -
    this was already the script's real behavior before this
    refactor and is not something this fix changes.
    """

    print("=" * 80)
    print("UNISCHED AI - PDF IMPORTER TEST")
    print("=" * 80)


    # ==========================================================
    # CHANGE THIS TO ONE OF YOUR PDF FILES
    # ==========================================================

    FILE = r"data\Facultywise TT 20 sep.pdf"


    # ==========================================================
    # 1. VALIDATION
    # ==========================================================

    print("\n1. FILE VALIDATION")
    print("-" * 80)

    validation = PDFImporter.validate_file(FILE)

    for key, value in validation.items():

        print(
            f"{key}: {value}"
        )


    if not validation["valid"]:

        print(
            "\nPDF VALIDATION FAILED"
        )

        return 0


    # ==========================================================
    # 2. PDF INSPECTION
    # ==========================================================

    print("\n2. PDF INSPECTION")
    print("-" * 80)

    info = PDFImporter.inspect_file(
        FILE
    )

    for key, value in info.items():

        print(
            f"{key}: {value}"
        )


    # ==========================================================
    # 3. RAW TABLE EXTRACTION
    # ==========================================================

    print("\n3. RAW TABLE EXTRACTION")
    print("-" * 80)

    records = PDFImporter.import_file(
        FILE
    )

    print(
        "Extracted records:",
        len(records)
    )


    # ==========================================================
    # 4. FIRST FIVE RECORDS
    # ==========================================================

    print("\n4. FIRST 5 RAW RECORDS")
    print("-" * 80)

    for index, record in enumerate(
        records[:5],
        start=1
    ):

        print(
            f"\nRECORD {index}"
        )

        print(
            "-" * 40
        )

        for key, value in record.items():

            print(
                f"{key}: {value}"
            )


    # ==========================================================
    # FINAL RESULT
    # ==========================================================

    print("\n" + "=" * 80)

    if records:

        print(
            "PDF IMPORTER TEST PASSED"
        )

        print(
            f"Extracted {len(records)} raw table records."
        )

    else:

        print(
            "PDF IMPORTER FOUND NO TABLE RECORDS."
        )

        print(
            "This does NOT necessarily mean the PDF is empty."
        )

        print(
            "It may use a layout that requires the next"
        )

        print(
            "PDF layout/OCR extraction layer."
        )

    print("=" * 80)
    return 0


def test_pdf_importer_extraction():
    """
    Pytest entry point. Runs the exact same PDF importer
    inspection/extraction test as
    `python test_pdf_importer.py` (via main()) and asserts it
    completed successfully. main() never raises SystemExit, so
    this behaves as a normal pytest test rather than aborting
    the test process.
    """

    exit_code = main()

    assert exit_code == 0, "pdf importer extraction test failed "\
        "(see printed output above for details)"


if __name__ == "__main__":
    sys.exit(main())