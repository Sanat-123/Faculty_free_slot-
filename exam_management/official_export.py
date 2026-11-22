"""
Official Excel / PDF export of an examination duty roster.

Built from the SAME payload the dashboard shows (``dashboard.build``), so the downloaded files can never differ
from what the coordinator reviewed.  Contents: examination summary, date-wise / shift-wise / room-wise roster with
departments, faculty duty statistics, eligibility report, unfilled positions and the validation report.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

HEAD = "305496"
STATUS_FILL = {"Passed": "C6EFCE", "Failed": "FFC7CE", "Not verified": "FFEB9C", "Not applicable": "EDEDED"}


def _banner(p: Dict[str, Any]) -> List[str]:
    note = "   (DRAFT - not approved)" if p["status"] == "draft" else ""
    return [f"{p['title']}  |  Allocation version {p['version']}",
            f"Status: {p['status_text']}{note}  |  Generated {datetime.now():%d %b %Y %H:%M} from database records"]


def to_excel(p: Dict[str, Any], path: Any) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    wb.remove(wb.active)
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=HEAD)

    def sheet(title: str):
        ws = wb.create_sheet(title[:31])
        for i, line in enumerate(_banner(p)):
            ws.cell(i + 1, 1, line).font = Font(bold=(i == 0), size=12 if i == 0 else 10)
        return ws

    def table(ws, headers, rows, start):
        for j, h in enumerate(headers, 1):
            c = ws.cell(start, j, h)
            c.font, c.fill, c.alignment = head_font, head_fill, Alignment(wrap_text=True, vertical="top")
        for i, r in enumerate(rows, 1):
            for j, v in enumerate(r, 1):
                ws.cell(start + i, j, v).alignment = Alignment(wrap_text=True, vertical="top")
        for j, h in enumerate(headers, 1):
            width = max([len(str(h))] + [len(str(r[j - 1])) for r in rows[:300] if j - 1 < len(r)])
            col = ws.cell(start, j).column_letter
            ws.column_dimensions[col].width = max(ws.column_dimensions[col].width or 0, min(max(width + 2, 10), 46))
        return start + len(rows) + 2

    ws = sheet("Examination summary")
    r = table(ws, ["Item", "Value"], [[c["label"], c["value"]] for c in p["cards"]], 4)
    ws.cell(r, 1, "What was understood").font = Font(bold=True)
    table(ws, ["Item", "Value", "Source"], [[u["item"], u["value"], u["source"]] for u in p["understood"]], r + 1)

    ws = sheet("Roster (date - shift - room)")
    row = 4
    for day in p["roster"]:
        for sh in day["shifts"]:
            ws.cell(row, 1, f"{day['label']} ({day['weekday']})   -   {sh['label']}   {sh['time']}").font = Font(bold=True, size=11)
            row = table(ws, sh["headers"], sh["rows"], row + 1)

    ws = sheet("Faculty duty distribution")
    table(ws, p["duty_distribution"]["headers"], p["duty_distribution"]["rows"], 4)
    ws.cell(len(p["duty_distribution"]["rows"]) + 6, 1, p["counting_note"])

    ws = sheet("Faculty eligibility report")
    table(ws, p["eligibility"]["headers"], p["eligibility"]["rows"], 4)

    ws = sheet("Unfilled positions")
    if p["unfilled"]["rows"]:
        table(ws, p["unfilled"]["headers"], p["unfilled"]["rows"], 4)
    else:
        ws.cell(4, 1, "None - every required position is filled.")

    ws = sheet("Validation report")
    end = table(ws, ["Check", "Status", "Detail"], [[c["check"], c["status"], c["detail"]] for c in p["validation"]], 4)
    for i, c in enumerate(p["validation"], 5):
        ws.cell(i, 2).fill = PatternFill("solid", fgColor=STATUS_FILL.get(c["status"], "FFFFFF"))
    for w in wb.worksheets:
        w.freeze_panes = "A5"
    path = Path(path)
    wb.save(path)
    return path


def to_pdf(p: Dict[str, Any], path: Any) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    st = getSampleStyleSheet()
    small = st["BodyText"].clone("cell", fontSize=7, leading=8.4)
    esc = lambda x: str(x).replace("&", "&amp;").replace("<", "&lt;")
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24,
                            title=p["title"])

    def tbl(headers, rows, status_col=None):
        data = [[Paragraph(f"<b>{esc(h)}</b>", small) for h in headers]] + \
               [[Paragraph(esc(c), small) for c in r] for r in rows]
        if len(data) == 1:
            data.append([Paragraph("(none)", small)] + [""] * (len(headers) - 1))
        t = LongTable(data, repeatRows=1)
        style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9e1f2")), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")])]
        if status_col is not None:
            for i, r in enumerate(rows, 1):
                style.append(("BACKGROUND", (status_col, i), (status_col, i), colors.HexColor("#" + STATUS_FILL.get(str(r[status_col]), "FFFFFF"))))
        t.setStyle(TableStyle(style))
        return t

    story: List[Any] = [Paragraph(f"<b>{esc(p['title'])}</b>", st["Title"])]
    story += [Paragraph(esc(b), st["BodyText"]) for b in _banner(p)] + [Spacer(1, 8)]
    story += [Paragraph("<b>Examination summary</b>", st["Heading2"]), tbl(["Item", "Value"], [[c["label"], c["value"]] for c in p["cards"]]),
              Spacer(1, 8), Paragraph("<b>What was understood</b>", st["Heading3"]),
              tbl(["Item", "Value", "Source"], [[u["item"], u["value"], u["source"]] for u in p["understood"]])]
    story.append(PageBreak())
    story.append(Paragraph("<b>Duty roster - date, shift and room</b>", st["Heading2"]))
    for day in p["roster"]:
        for sh in day["shifts"]:
            story += [Spacer(1, 6), Paragraph(f"<b>{esc(day['label'])} ({day['weekday']}) - {esc(sh['label'])}: {esc(sh['time'])}</b>"
                                              + (f"  <font color='red'>({sh['unfilled']} unfilled)</font>" if sh["unfilled"] else ""), st["Heading4"]),
                      tbl(sh["headers"], sh["rows"])]
    for title, key in (("Faculty duty distribution", "duty_distribution"), ("Faculty eligibility report", "eligibility")):
        story += [PageBreak(), Paragraph(f"<b>{title}</b>", st["Heading2"]), tbl(p[key]["headers"], p[key]["rows"])]
        if key == "duty_distribution":
            story.append(Paragraph(esc(p["counting_note"]), small))
    story += [PageBreak(), Paragraph("<b>Unfilled positions</b>", st["Heading2"]), tbl(p["unfilled"]["headers"], p["unfilled"]["rows"]),
              Spacer(1, 10), Paragraph(f"<b>Validation report - overall: {esc(p['validation_overall'])}</b>", st["Heading2"]),
              tbl(["Check", "Status", "Detail"], [[c["check"], c["status"], c["detail"]] for c in p["validation"]], status_col=1)]
    doc.build(story)
    return Path(path)