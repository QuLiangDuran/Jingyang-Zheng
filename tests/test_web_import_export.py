"""Review page: upload a paper (local conversion only), start extraction by hand, export with a scope."""
import io
import json
import time

import pymupdf
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from audit.api import create_app
from audit.db import connect
from audit.export import export_rows
from audit.llm import ReplayClient
from tests.conftest import load_demo

ANSWER = {"source": {"doi": {"value": "10.1/x", "quote": None}},
          "conditions": [{"condition_key": "c1", "organism_name_reported": {"value": "Chlorella", "quote": "Chlorella"}}],
          "observations": [{"condition_ref": "c1", "metric_code": {"value": "dry_biomass"},
                            "value_original": {"value": "1.2 g/L", "quote": "1.2 g/L"}}]}


def _pdf_bytes(text="Results. Chlorella reached 1.2 g/L of dry biomass."):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    for i in range(30):  # enough text not to look like a scanned PDF
        page.insert_text((72, 100 + 20 * i), f"Line {i} of the methods section describes the culture setup.")
    return doc.tobytes()


def _client(tmp_path, llm=None):
    calls = []

    def factory():
        calls.append(1)
        return llm or ReplayClient([json.dumps(ANSWER)])
    app = create_app(tmp_path / "t.db", papers_dir=tmp_path / "papers", llm_factory=factory)
    return TestClient(app), calls


def _wait(client, timeout=10):
    t = time.time()
    while time.time() - t < timeout:
        job = client.get("/api/jobs/current").json()
        if job and job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# ---------- upload: local PDF conversion, no API call ----------

def test_upload_pdf_converts_locally_and_does_not_call_the_model(tmp_path):
    client, calls = _client(tmp_path)
    r = client.post("/api/documents/upload", data={"title": "2024_TEST"},
                    files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"] == "2024_TEST" and d["pages"] == 1 and d["chars"] > 20
    assert "1.2 g/L" in d["preview"] and calls == []                       # nothing sent to the model
    assert (tmp_path / "papers" / "2024_TEST.pdf").is_file()
    docs = client.get("/api/documents").json()
    assert docs[0]["title"] == "2024_TEST" and docs[0]["runs"] == 0 and docs[0]["has_pdf"]


def test_upload_title_defaults_to_file_name_and_keeps_existing_files(tmp_path):
    client, _ = _client(tmp_path)
    same = _pdf_bytes()  # PyMuPDF stamps every new file, so build the bytes once
    for _ in range(2):
        r = client.post("/api/documents/upload", files={"file": ("2017_NG.pdf", same, "application/pdf")})
        assert r.json()["title"] == "2017_NG"
    other = client.post("/api/documents/upload", files={"file": ("2017_NG.pdf", _pdf_bytes("Other paper 5 mV."), "application/pdf")})
    assert other.status_code == 200
    assert sorted(p.name for p in (tmp_path / "papers").iterdir()) == ["2017_NG-2.pdf", "2017_NG.pdf"]  # same bytes reused


def test_upload_txt_and_refuse_other_types(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/api/documents/upload", files={"file": ("p.txt", "Results. It gave 5 mV.".encode(), "text/plain")})
    assert r.status_code == 200 and r.json()["pages"] is None
    bad = client.post("/api/documents/upload", files={"file": ("p.docx", b"x", "application/octet-stream")})
    assert bad.status_code == 400


def test_upload_of_a_scanned_pdf_is_refused(tmp_path):
    client, _ = _client(tmp_path)
    blank = pymupdf.open()
    blank.new_page()
    r = client.post("/api/documents/upload", files={"file": ("scan.pdf", blank.tobytes(), "application/pdf")})
    assert r.status_code == 400 and client.get("/api/documents").json() == []


# ---------- extraction starts only when asked ----------

def test_extract_runs_in_the_background_and_reports(tmp_path):
    client, calls = _client(tmp_path)
    doc = client.post("/api/documents/upload", files={"file": ("p.txt", b"Chlorella reached 1.2 g/L.", "text/plain")}).json()
    r = client.post(f"/api/documents/{doc['id']}/extract")
    assert r.status_code == 200 and r.json()["status"] in ("running", "done")
    job = _wait(client)
    assert job["status"] == "done" and job["document_id"] == doc["id"] and calls == [1]
    assert job["summary"]["records"] == 3 and job["summary"]["failed_runs"] == 0
    docs = client.get("/api/documents").json()
    assert docs[0]["total"] == 3 and docs[0]["runs"] == 1


def test_only_one_extraction_at_a_time(tmp_path):
    import threading
    gate = threading.Event()

    class Slow(ReplayClient):
        def complete(self, *a, **k):
            gate.wait(5)
            return json.dumps(ANSWER)
    client, _ = _client(tmp_path, llm=Slow([]))
    ids = [client.post("/api/documents/upload", files={"file": (f"p{i}.txt", b"Chlorella 1.2 g/L.", "text/plain")}).json()["id"]
           for i in (1, 2)]
    assert client.post(f"/api/documents/{ids[0]}/extract").status_code == 200
    assert client.post(f"/api/documents/{ids[1]}/extract").status_code == 409
    gate.set()
    assert _wait(client)["status"] == "done"


def test_extract_failure_is_reported(tmp_path):
    class Broken(ReplayClient):
        def complete(self, *a, **k):
            raise RuntimeError("401 invalid api key")
    client, _ = _client(tmp_path, llm=Broken([]))
    doc = client.post("/api/documents/upload", files={"file": ("p.txt", b"Chlorella 1.2 g/L.", "text/plain")}).json()
    client.post(f"/api/documents/{doc['id']}/extract")
    job = _wait(client)
    assert job["status"] == "done" and job["summary"]["failed_runs"] == 1 and "401" in job["message"]


def test_extract_unknown_document_is_404(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/documents/99/extract").status_code == 404


# ---------- export scope ----------

def test_export_all_includes_unreviewed_but_not_rejected(conn, demo):
    assert export_rows(conn) == []                                            # nothing approved yet
    rows = export_rows(conn, scope="all")
    obs = conn.execute("SELECT COUNT(*) FROM records WHERE kind='observation' AND parent_id IS NOT NULL").fetchone()[0]
    assert len(rows) == obs and {r["review_status"] for r in rows} == {"pending"}
    assert {"condition_review_status", "source_review_status"} <= set(rows[0])
    from audit import review_service
    review_service.reject(conn, rows[0]["observation_record_id"], "t", "wrong")
    assert len(export_rows(conn, scope="all")) == obs - 1


def test_export_endpoints_take_scope_and_document(tmp_path):
    db = tmp_path / "t.db"
    c = connect(db)
    a, b = load_demo(c), load_demo(c)
    c.close()
    client = TestClient(create_app(db, papers_dir=tmp_path / "papers"))
    r = client.get("/api/export.csv?scope=all")
    assert r.status_code == 200 and "all" in r.headers["content-disposition"]
    n_all = r.content.decode("utf-8-sig").strip().count("\n")
    one = client.get(f"/api/export.csv?scope=all&document_id={a}").content.decode("utf-8-sig").strip().count("\n")
    assert n_all == 2 * one and one > 0
    wb = load_workbook(io.BytesIO(client.get(f"/api/export.xlsx?scope=all&document_id={b}").content))
    assert wb["observations"].max_row - 1 == one and wb["sources"].max_row == 2
    assert client.get("/api/export.csv?scope=bogus").status_code == 422
