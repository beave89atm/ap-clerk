"""Excel run report writer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ap_clerk.quality_v12 import (
    COL_EXCEPTION_CATEGORY,
    COL_EXCEPTION_OWNER,
    apply_exception_category_owner,
    exception_category_counts,
)

COLUMNS = [
    "Vendor",
    "Invoice #",
    "date",
    "PO",
    "Amount",
    "Result",
    "Why",
    COL_EXCEPTION_CATEGORY,
    COL_EXCEPTION_OWNER,
    "KIMCO id",
    "Batch",
    "Fees and surcharges",
    "PPV",
    "Attach status",
    "Flag in Outlook",
    "Flag status",
    "Notes",
]


def write_report(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = [apply_exception_category_owner(dict(row)) for row in rows]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "AP run"
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    for col, name in enumerate(COLUMNS, start=1):
        cell = sheet.cell(1, col, name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(wrap_text=True)
    fills = {
        "Success": PatternFill("solid", fgColor="C6EFCE"),
        "Incomplete": PatternFill("solid", fgColor="F8CBAD"),
        "Fail": PatternFill("solid", fgColor="FFC7CE"),
        "HOLD": PatternFill("solid", fgColor="FFEB9C"),
        "Skipped": PatternFill("solid", fgColor="D9D9D9"),
        "Noise": PatternFill("solid", fgColor="D9D9D9"),
    }
    for row_idx, row in enumerate(stamped, start=2):
        values = ["" if col == "Notes" else row.get(col, "") for col in COLUMNS]
        for col, value in enumerate(values, start=1):
            cell = sheet.cell(row_idx, col, value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if COLUMNS[col - 1] == "Result":
                fill = fills.get(str(value))
                if fill:
                    cell.fill = fill
    widths = [28, 18, 12, 12, 12, 12, 55, 18, 24, 12, 22, 40, 10, 16, 18, 18, 18]
    for idx, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(idx)].width = width
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(1, len(rows) + 1)}"
    sheet.freeze_panes = "A2"
    sheet.row_dimensions[1].height = 22
    _write_exception_counts_sheet(workbook, stamped, header_font, header_fill)
    workbook.save(path)
    return path


def _write_exception_counts_sheet(
    workbook: Workbook,
    rows: list[dict[str, Any]],
    header_font: Font,
    header_fill: PatternFill,
) -> None:
    """Counts by Exception category only. Never invent Success/touchless rates."""
    sheet = workbook.create_sheet("Exception counts")
    for col, name in enumerate(("Exception category", "Count"), start=1):
        cell = sheet.cell(1, col, name)
        cell.font = header_font
        cell.fill = header_fill
    for row_idx, (category, count) in enumerate(exception_category_counts(rows), start=2):
        sheet.cell(row_idx, 1, category)
        sheet.cell(row_idx, 2, count)
    sheet.column_dimensions["A"].width = 22
    sheet.column_dimensions["B"].width = 10
    sheet.freeze_panes = "A2"
