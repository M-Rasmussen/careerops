from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.services.excel import HEADERS
from tests.test_applications import make_app, make_job


def _sheet(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.headers["content-disposition"].startswith('attachment; filename="applications_')
    return load_workbook(BytesIO(response.content)).active


def test_export_contains_expected_columns_and_values(client: TestClient) -> None:
    make_app(
        client,
        make_job(client, "Export Co", "Data Engineer"),
        applied_date="2026-11-03",
        follow_up_date="2026-11-10",
        status="interviewing",
        notes="Phone screen booked",
    )
    ws = _sheet(client.get("/api/applications/export.xlsx", params={"company": "Export Co"}))
    rows = list(ws.iter_rows(values_only=True))
    assert list(rows[0]) == HEADERS
    assert len(rows) == 2
    company, title, applied, status, url, follow_up, notes = rows[1]
    assert (company, title, status, url, notes) == (
        "Export Co",
        "Data Engineer",
        "interviewing",
        "https://jobs.example.com/1",
        "Phone screen booked",
    )
    assert applied.date().isoformat() == "2026-11-03"
    assert follow_up.date().isoformat() == "2026-11-10"


def test_export_applies_filters_and_handles_empty(client: TestClient) -> None:
    make_app(client, make_job(client, "Only Co"), status="offer")
    rows = list(
        _sheet(
            client.get("/api/applications/export.xlsx", params={"company": "Only Co", "status": "rejected"})
        ).iter_rows(values_only=True)
    )
    assert rows == [tuple(HEADERS)]


def test_export_neutralizes_formula_injection(client: TestClient) -> None:
    make_app(client, make_job(client, "=HYPERLINK(\"http://evil\")"), notes="=1+1")
    response = client.get("/api/applications/export.xlsx", params={"company": "HYPERLINK"})
    ws = _sheet(response)
    for cell in (ws["A2"], ws["G2"]):
        assert cell.data_type == "s"
    assert ws["G2"].value == "=1+1"
    assert b"<f>" not in ZipFile(BytesIO(response.content)).read("xl/worksheets/sheet1.xml")


def test_export_rejects_bad_filter(client: TestClient) -> None:
    assert client.get("/api/applications/export.xlsx", params={"status": "nope"}).status_code == 422
