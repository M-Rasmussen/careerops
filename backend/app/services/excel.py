"""Excel export of applications."""

from io import BytesIO

from openpyxl import Workbook

from app.models import Application

HEADERS = ["Company", "Job Title", "Application Date", "Status", "Posting URL", "Follow-up Date", "Notes"]


def applications_to_xlsx(applications: list[Application]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Applications"
    ws.append(HEADERS)
    for a in applications:
        ws.append([
            a.job.company,
            a.job.title,
            a.applied_date,
            a.status,
            a.job.posting_url,
            a.follow_up_date,
            a.notes,
        ])
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str):
                # openpyxl turns strings starting with "=" into formulas; force plain text (formula injection).
                cell.data_type = "s"
            elif cell.value is not None:
                cell.number_format = "yyyy-mm-dd"
    ws.freeze_panes = "A2"
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
