from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from import_engine.pdf_importer import PDFImporter
from import_engine.excel_importer import ExcelImporter
from import_engine.csv_importer import CSVImporter
from import_engine.universal_normalizer import UniversalNormalizer


class ImportManager:
    """
    Universal timetable import manager.

    Supported formats:
        - PDF
        - XLSX
        - XLS
        - CSV

    Architecture:

        Source File
             |
             v
        Source Importer
             |
             v
        UniversalNormalizer
             |
             v
        Canonical Records

    Important:
        The manager normalizes existing information.
        It does NOT invent missing day, slot, room, teacher,
        class, or subject information.
    """

    # ======================================================
    # SUPPORTED FILE TYPES
    # ======================================================

    SUPPORTED_EXTENSIONS = {
        ".pdf": "pdf",
        ".xlsx": "xlsx",
        ".xls": "xls",
        ".csv": "csv",
    }

    # ======================================================
    # CONSTRUCTOR
    # ======================================================

    def __init__(self):
        self.imported_files: List[str] = []
        self.failed_files: List[str] = []

    # ======================================================
    # DETECT FILE TYPE
    # ======================================================

    @staticmethod
    def detect_file_type(
        file_path: str | Path
    ) -> str:

        path = Path(file_path)

        extension = path.suffix.lower()

        return ImportManager.SUPPORTED_EXTENSIONS.get(
            extension,
            "unknown"
        )

    # ======================================================
    # VALIDATE FILE
    # ======================================================

    @staticmethod
    def validate_file(
        file_path: str | Path
    ) -> Dict[str, Any]:

        path = Path(file_path)

        # --------------------------------------------------
        # Does file exist?
        # --------------------------------------------------

        if not path.exists():

            return {
                "valid": False,
                "filename": path.name,
                "extension": path.suffix.lower(),
                "size_bytes": 0,
                "implemented": False,
                "reason": "File does not exist.",
            }

        # --------------------------------------------------
        # Must be a file
        # --------------------------------------------------

        if not path.is_file():

            return {
                "valid": False,
                "filename": path.name,
                "extension": path.suffix.lower(),
                "size_bytes": 0,
                "implemented": False,
                "reason": "Path is not a file.",
            }

        # --------------------------------------------------
        # Extension
        # --------------------------------------------------

        extension = path.suffix.lower()

        # --------------------------------------------------
        # File size
        # --------------------------------------------------

        size_bytes = path.stat().st_size

        if size_bytes <= 0:

            return {
                "valid": False,
                "filename": path.name,
                "extension": extension,
                "size_bytes": size_bytes,
                "implemented": (
                    extension
                    in ImportManager.SUPPORTED_EXTENSIONS
                ),
                "reason": "File is empty.",
            }

        # --------------------------------------------------
        # Supported?
        # --------------------------------------------------

        if extension not in ImportManager.SUPPORTED_EXTENSIONS:

            return {
                "valid": False,
                "filename": path.name,
                "extension": extension,
                "size_bytes": size_bytes,
                "implemented": False,
                "reason": (
                    "Unsupported file type: "
                    f"{extension}"
                ),
            }

        # --------------------------------------------------
        # Valid
        # --------------------------------------------------

        return {
            "valid": True,
            "filename": path.name,
            "extension": extension,
            "size_bytes": size_bytes,
            "implemented": True,
            "reason": None,
        }

    # ======================================================
    # NORMALIZE RECORDS
    # ======================================================

    @staticmethod
    def normalize_records(
        records: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Apply UniversalNormalizer to every imported record.

        Important:
            - Existing information is preserved.
            - Missing information remains missing.
            - No timetable values are invented.
            - Unknown/custom fields are preserved.

        This is the common normalization gateway for
        PDF, Excel and CSV data.
        """

        normalized_records: List[Dict[str, Any]] = []

        for record in records:

            if not isinstance(record, dict):
                continue

            normalized = UniversalNormalizer.normalize_record(
                record
            )

            normalized_records.append(
                normalized
            )

        return normalized_records

    # ======================================================
    # IMPORT FILE
    # ======================================================

    def import_file(
        self,
        file_path: str | Path
    ) -> Dict[str, Any]:
        """
        Import one timetable file.

        Returns:

            {
                "success": True,
                "filename": "...",
                "file_type": "pdf",
                "records": [...],
                "record_count": 123,
                "inspection": {...},
                "warnings": [...]
            }
        """

        path = Path(file_path)

        # --------------------------------------------------
        # Validate
        # --------------------------------------------------

        validation = self.validate_file(path)

        if not validation["valid"]:

            self.failed_files.append(
                path.name
            )

            return {
                "success": False,
                "filename": path.name,
                "file_type": self.detect_file_type(path),
                "records": [],
                "record_count": 0,
                "inspection": None,
                "warnings": [
                    validation["reason"]
                ],
                "error": validation["reason"],
            }

        # --------------------------------------------------
        # Detect type
        # --------------------------------------------------

        file_type = self.detect_file_type(path)

        try:

            inspection = None

            # ==================================================
            # PDF
            #
            # Deliberately does NOT call PDFImporter.inspect_
            # file() here: that method re-opens the PDF and
            # re-walks every page/table a second time purely for
            # diagnostics, roughly doubling PDF processing time
            # for a file type that is already the slowest to
            # import. Nothing downstream of this method actually
            # reads `inspection` for PDFs beyond has_day/has_slot,
            # which are computed a few lines below from the real
            # `records` for EVERY file type anyway. PDFImporter.
            # inspect_file() remains available to call directly
            # (e.g. for troubleshooting a specific file) - it is
            # just not part of the normal import path anymore.
            # ==================================================

            if file_type == "pdf":

                importer = PDFImporter()

                records = importer.import_file(
                    path
                )

            # ==================================================
            # EXCEL
            # ==================================================

            elif file_type in (
                "xlsx",
                "xls",
            ):

                importer = ExcelImporter()

                records = importer.import_file(
                    path
                )

                if hasattr(
                    importer,
                    "inspect_file"
                ):

                    inspection = importer.inspect_file(
                        path
                    )

            # ==================================================
            # CSV
            # ==================================================

            elif file_type == "csv":

                importer = CSVImporter()

                records = importer.import_file(
                    path
                )

                if hasattr(
                    importer,
                    "inspect_file"
                ):

                    inspection = importer.inspect_file(
                        path
                    )

            # ==================================================
            # UNKNOWN
            # ==================================================

            else:

                raise ValueError(
                    (
                        "Unsupported file type: "
                        f"{path.suffix}"
                    )
                )

            # --------------------------------------------------
            # Make sure records is a list
            # --------------------------------------------------

            if records is None:

                records = []

            elif isinstance(
                records,
                tuple
            ):

                records = list(
                    records
                )

            elif not isinstance(
                records,
                list
            ):

                records = list(
                    records
                )

            # --------------------------------------------------
            # UNIVERSAL NORMALIZATION
            # --------------------------------------------------

            raw_record_count = len(records)

            records = self.normalize_records(
                records
            )

            normalized_record_count = len(records)

            # --------------------------------------------------
            # Success
            # --------------------------------------------------

            self.imported_files.append(
                path.name
            )

            # --------------------------------------------------
            # Generate warnings
            # --------------------------------------------------

            warnings: List[str] = []

            # --------------------------------------------------
            # Inspection warnings
            # --------------------------------------------------

            if inspection:

                if (
                    inspection.get(
                        "has_day"
                    ) is False
                ):

                    warnings.append(
                        "Dataset does not contain Day information."
                    )

                if (
                    inspection.get(
                        "has_slot"
                    ) is False
                ):

                    warnings.append(
                        "Dataset does not contain Slot information."
                    )

            # --------------------------------------------------
            # Normalization warning
            # --------------------------------------------------

            if (
                raw_record_count
                != normalized_record_count
            ):

                warnings.append(
                    (
                        "Some invalid/non-dictionary records "
                        "were removed during normalization."
                    )
                )

            # --------------------------------------------------
            # Additional data-quality information
            # --------------------------------------------------

            missing_day = sum(
                1
                for record in records
                if not str(
                    record.get("day", "")
                ).strip()
            )

            missing_slot = sum(
                1
                for record in records
                if record.get("slot") is None
            )

            if records and missing_day == len(records):

                warnings.append(
                    "All records have missing Day information."
                )

            elif missing_day > 0:

                warnings.append(
                    (
                        f"{missing_day} of "
                        f"{len(records)} records "
                        "have missing Day information."
                    )
                )

            if records and missing_slot == len(records):

                warnings.append(
                    "All records have missing Slot information."
                )

            elif missing_slot > 0:

                warnings.append(
                    (
                        f"{missing_slot} of "
                        f"{len(records)} records "
                        "have missing Slot information."
                    )
                )

            # --------------------------------------------------
            # Return
            # --------------------------------------------------

            return {
                "success": True,
                "filename": path.name,
                "file_type": file_type,
                "records": records,
                "record_count": len(records),
                "raw_record_count": raw_record_count,
                "normalized_record_count": normalized_record_count,
                "inspection": inspection,
                "warnings": warnings,
                "error": None,
            }

        except Exception as error:

            self.failed_files.append(
                path.name
            )

            return {
                "success": False,
                "filename": path.name,
                "file_type": file_type,
                "records": [],
                "record_count": 0,
                "inspection": None,
                "warnings": [],
                "error": str(error),
            }

    # ======================================================
    # IMPORT MULTIPLE FILES
    # ======================================================

    def import_files(
        self,
        file_paths: List[
            str | Path
        ]
    ) -> Dict[str, Any]:
        """
        Import multiple user files.

        Supported combinations include:

            PDF + PDF
            PDF + Excel
            PDF + CSV
            Excel + CSV
            PDF + Excel + CSV

        All successful records pass through the same
        UniversalNormalizer.
        """

        results: List[
            Dict[str, Any]
        ] = []

        all_records: List[
            Dict[str, Any]
        ] = []

        for file_path in file_paths:

            result = self.import_file(
                file_path
            )

            results.append(
                result
            )

            if result["success"]:

                all_records.extend(
                    result["records"]
                )

        successful_count = sum(
            1
            for result in results
            if result["success"]
        )

        failed_count = (
            len(results)
            - successful_count
        )

        return {
            "success": (
                all(
                    result["success"]
                    for result in results
                )
                if results
                else False
            ),

            "files": results,

            "records": all_records,

            "record_count": len(
                all_records
            ),

            "file_count": len(
                results
            ),

            "successful_files": successful_count,

            "failed_files": failed_count,
        }

    # ======================================================
    # RESET
    # ======================================================

    def reset(self):

        self.imported_files.clear()

        self.failed_files.clear()

    # ======================================================
    # SUMMARY
    # ======================================================

    def summary(self) -> Dict[str, Any]:
        """
        Return a simple import manager summary.
        """

        return {
            "imported_files": list(
                self.imported_files
            ),
            "failed_files": list(
                self.failed_files
            ),
            "imported_count": len(
                self.imported_files
            ),
            "failed_count": len(
                self.failed_files
            ),
        }


# ==========================================================
# DIRECT TEST
# ==========================================================

if __name__ == "__main__":

    print("=" * 80)
    print("UNISCHED AI - IMPORT MANAGER TEST")
    print("=" * 80)

    manager = ImportManager()

    print("\nSupported file types:")

    for extension, file_type in (
        manager.SUPPORTED_EXTENSIONS.items()
    ):
        print(
            f"  {extension:6} -> {file_type}"
        )

    print("\nManager initialized successfully.")

    print("\nSummary:")

    print(
        manager.summary()
    )

    print("\nImportManager test completed.")