import io

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from audit.api import create_app
from audit.db import connect
from tests.conftest import load_demo


def test_end_to_end(tmp_path):
    db = tmp_path / "t.db"
    c = connect(db)
    load_demo(c)
    c.close()
    client = TestClient(create_app(db))

    assert client.get("/").status_code == 200
    meta = client.get("/api/meta").json()
    assert meta["fields"][0]["name"] == "study_id" and meta["schema_version"] == "microalgae-1"
    assert "habitat_class" in meta["temp_choice_fields"]
    assert len(client.get("/api/records?status=pending").json()) == 8
    assert [r["id"] for r in client.get("/api/records?kind=observation&flagged=false").json()] == [4, 6]

    d = client.get("/api/records/5").json()
    assert d["kind"] == "observation" and next(f for f in d["fields"] if f["name"] == "analyte")["highlight"]

    r = client.post("/api/records/5/edit", json={"reviewer": "张三", "field": "value_numeric", "value": "0.97",
                                                 "reason": "typo"})
    assert r.status_code == 200 and next(f for f in r.json()["fields"] if f["name"] == "value_numeric")["value"] == "0.97"
    bad = client.post("/api/records/3/edit", json={"reviewer": "张三", "field": "habitat_class",
                                                   "value": "pond", "reason": "x"})
    assert bad.status_code == 400
    for rid in (1, 3, 5):
        assert client.post(f"/api/records/{rid}/approve", json={"reviewer": "张三"}).json()["status"] == "approved"
    assert client.post("/api/records/5/approve", json={"reviewer": "张三"}).status_code == 409
    assert client.post("/api/records/7/flag", json={"reviewer": "张三", "reason": "问作者"}).json()["status"] \
        == "needs_resolution"
    assert client.post("/api/records/6/reject", json={"reviewer": "张三", "reason": ""}).status_code == 400
    assert client.get("/api/records/999").status_code == 404

    csv = client.get("/api/export.csv")
    assert csv.content.startswith(b"\xef\xbb\xbf") and "0.97" in csv.content.decode("utf-8-sig")

    wb = load_workbook(io.BytesIO(client.get("/api/export.xlsx").content))
    rows = list(wb["observations"].values)
    assert rows[0][0] == "study_id" and len(rows) == 2 and rows[1][0] == "2025_EXAMPLE"
