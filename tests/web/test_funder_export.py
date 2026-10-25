"""The funder packet's CSV and PDF (`krater.web.funder_export`). Members wrote most of the text in them, so a
formula or markup in a title must not run, and a character the PDF font lacks must not break the file."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime

from krater.models import ProjectStatus
from krater.services.funder_packet import GpuEstimate, Packet, PacketRow
from krater.web import funder_export


def _row(**fields) -> PacketRow:
    values = {
        "project_id": uuid.uuid4(),
        "title": "Rover",
        "summary": "Maps caves.",
        "compute_use": "Training a mapping model.",
        "status": ProjectStatus.APPROVED,
        "requested_cents": 12_345,
        "granted_cents": 10_000,
        "gpu": GpuEstimate(count=2, name="A100", hours=40.0, interruptible=False),
        "team_names": ["Ada Lead", "Bea"],
        "unnamed": 0,
        "team_size": 2,
    }
    return PacketRow(**{**values, **fields})


def _packet(*rows: PacketRow) -> Packet:
    return Packet(
        funder="ospool",
        generated_at=datetime(2026, 10, 25, 12, tzinfo=UTC),
        include_pending=False,
        rows=list(rows),
        left_out=0,
    )


def test_the_csv_has_one_row_per_project_with_dollar_amounts() -> None:
    rows = list(csv.reader(io.StringIO(funder_export.to_csv(_packet(_row())))))

    assert rows[0] == list(funder_export.CSV_COLUMNS)
    assert rows[1] == [
        "Rover",
        "Maps caves.",
        "Training a mapping model.",
        "Approved",
        "123.45",
        "100.00",
        "2× A100 × 40h, on-demand",
        "Ada Lead, Bea",
        "2",
        "Every team member is a verified US resident",
    ]


def test_a_formula_in_member_text_does_not_run_in_a_spreadsheet() -> None:
    row = _row(title='=HYPERLINK("https://evil.example")', summary="+1", compute_use="@SUM(A1)")

    [_, cells] = list(csv.reader(io.StringIO(funder_export.to_csv(_packet(row)))))

    assert cells[0].startswith("'=")
    assert cells[1] == "'+1"
    assert cells[2] == "'@SUM(A1)"


def test_unnamed_members_are_counted_not_named() -> None:
    assert funder_export.team_text(_row(team_names=["Ada Lead"], unnamed=2)) == "Ada Lead and 2 members not named"
    assert funder_export.team_text(_row(team_names=[], unnamed=1)) == "1 member not named"


def test_the_pdf_is_a_pdf_even_with_markup_and_characters_the_font_lacks() -> None:
    row = _row(title="<b>Rover</b> & co", summary="Caves 洞窟 🚀\nSecond line", gpu=None)

    pdf = funder_export.to_pdf(_packet(row, _row(title="Second")))

    assert pdf.startswith(b"%PDF-")
    assert b"%%EOF" in pdf[-1024:]


def test_the_file_name_says_who_and_when() -> None:
    assert funder_export.filename(_packet(), "pdf") == "ganymede-ospool-2026-10-25.pdf"
