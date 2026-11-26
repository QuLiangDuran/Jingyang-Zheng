import csv
import io
from pathlib import Path

import pytest
from openpyxl import load_workbook

from audit import review_service as svc
from audit.export import export_columns, export_rows, to_csv_bytes, to_xlsx_bytes
from audit.fields import PAPER_FIELDS

TEMPLATE = Path(__file__).resolve().parent.parent / "docs" / "Microalgae_Data.xlsx"


def _template_header() -> list[str]:
    ws = load_workbook(TEMPLATE, read_only=True)["Extraction Template"]
    return [v for v in list(ws.iter_rows(values_only=True))[1] if v]  # skips the unnamed "Morphology" column


@pytest.mark.skipif(not TEMPLATE.exists(), reason="docs/Microalgae_Data.xlsx is kept local (not in git)")
def test_export_columns_follow_extraction_template():
    expected = ["statistic_type" if h == "statistice_type" else h for h in _template_header()]
    expected.insert(expected.index("metric_code") + 1, "record_role")
    expected.insert(expected.index("mediator_concentration") + 1, "mediator_concentration_unit")
    expected.insert(expected.index("applied_potential_value"), "potential_applied")   # new in microalgae-2
    expected += [f.name for f in PAPER_FIELDS]
    cols = export_columns()
    assert len(expected) == 160
    assert cols[:len(expected)] == expected
    rest = cols[len(expected):]
    assert all(c.endswith("_quote") for c in rest[:-11]) and "value_numeric_quote" in rest   # 11 extra columns (2 review statuses added)


def _approve(conn, *ids):
    for i in ids:
        svc.approve(conn, i, "a")


def test_only_fully_approved_rows_are_exported(conn, demo):
    assert export_rows(conn) == []
    _approve(conn, 4)                       # observation only
    assert export_rows(conn) == []
    _approve(conn, 2)                       # + its condition
    assert export_rows(conn) == []
    _approve(conn, 1)                       # + the paper
    assert [r["observation_record_id"] for r in export_rows(conn)] == [4]

    svc.edit_field(conn, 5, "a", "value_numeric", "typo", value="0.97")
    _approve(conn, 5)                       # its condition (#3) is still pending
    svc.reject(conn, 6, "a", "no")          # rejected
    svc.flag_for_resolution(conn, 7, "a", "ask")
    assert [r["observation_record_id"] for r in export_rows(conn)] == [4]
    _approve(conn, 3)
    assert [r["observation_record_id"] for r in export_rows(conn)] == [4, 5]


def test_row_content(conn, demo):
    svc.edit_field(conn, 4, "a", "n_biological", "Methods 写了三个独立培养", value="3")
    _approve(conn, 1, 2, 4, 6)
    r4, r6 = export_rows(conn)
    assert (r4["study_id"], r4["source_id"], r4["condition_id"], r4["observation_id"]) == \
        ("2025_EXAMPLE", "2025_EXAMPLE-S1", "2025_EXAMPLE-C1", "2025_EXAMPLE-O1")
    assert r4["experiment_id"] == "2025_EXAMPLE-E1" and r4["arm_id"] == "2025_EXAMPLE-A1"
    assert r4["doi"] == "10.0000/demo.2025.001" and r4["genus"] == "Chlorella"
    assert r4["value_numeric"] == "1.42" and r4["value_numeric_quote"].startswith("dry biomass was 1.42")
    assert r4["n_biological"] == "3" and r4["edited"] is True and r6["edited"] is False
    assert (r4["review_status"], r4["reviewer_id"]) == ("approved", "a") and r4["reviewed_at"]
    assert r4["reviewer_notes"] == "[edit n_biological] Methods 写了三个独立培养"
    # missing policy
    assert r4["parent_source_id"] == "N/A" and r4["salinity_value"] == "N/A"
    assert r4["accepted_taxon_name"] is None and r4["availability_status"] is None   # "later" fields
    assert r4["is_electrical"] is False and r4["electrode_area_value"] == "N/A"      # not an electrical row
    assert r6["is_electrical"] is True and r6["electrode_area_value"] is None         # electrical, policy "—"
    assert r4["anode_material"] == "indium tin oxide"                                 # real values kept
    assert r4["strain_id"] == r4["strain_accession_reported"] == "DEMO-17"           # copied by the system
    assert r6["potential_applied"] == "unknown"
    assert r4["condition_source_locator"] == "Methods"


def test_csv_has_bom_and_xlsx_has_sources_sheet(conn, demo):
    _approve(conn, 1, 2, 4)
    data = to_csv_bytes(conn)
    assert data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0] == export_columns() and len(rows) == 2 and "±" in data.decode("utf-8-sig")

    wb = load_workbook(io.BytesIO(to_xlsx_bytes(conn)))
    assert wb.sheetnames == ["observations", "sources"]
    obs = list(wb["observations"].values)
    assert list(obs[0]) == export_columns() and len(obs) == 2
    src = list(wb["sources"].values)
    head = list(src[0])
    assert len(src) == 2 and src[1][head.index("paper_verdict")] == "include"
    assert src[1][head.index("exported_rows")] == 1
