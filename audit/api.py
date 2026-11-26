"""FastAPI app: thin HTTP wrapper around review_service and export."""
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import re
from urllib.parse import quote as urlquote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import export, review_service as svc
from .config import PROJECT_ROOT, load_settings
from .db import connect
from .extract import PROMPT_VERSION
from .fields import FIELDS, LEVEL_LABELS, SCHEMA_VERSION, TEMP_CHOICES
from .web_import import ExtractionJobs, ImportError_, import_file, save_upload

STATIC_DIR = Path(__file__).parent / "static"


class ApproveIn(BaseModel):
    reviewer: str
    note: str | None = None


class ReasonIn(BaseModel):
    reviewer: str
    reason: str


class EditIn(BaseModel):
    reviewer: str
    field: str
    reason: str
    value: str | None = None  # None = unchanged, "" = clear
    quote: str | None = None


def _default_llm():
    from .llm import OpenAICompatClient
    return OpenAICompatClient.from_settings(load_settings())  # read .env again for every extraction


def create_app(db_path: str | Path | None = None, papers_dir: str | Path | None = None,
               llm_factory=None) -> FastAPI:
    """`papers_dir`: where uploaded papers are saved (default papers/). `llm_factory`: builds the model
    client for an extraction started from the page (tests pass a fake one)."""
    db_path = db_path or load_settings().db_path
    papers_dir = Path(papers_dir or PROJECT_ROOT / "papers")
    jobs = ExtractionJobs(db_path, llm_factory or _default_llm)
    app = FastAPI(title="Audit")

    def get_conn() -> Iterator:
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except svc.NotFound as e:
            raise HTTPException(404, str(e))
        except svc.Conflict as e:
            raise HTTPException(409, str(e))
        except svc.Invalid as e:
            raise HTTPException(400, str(e))

    @app.get("/api/meta")
    def meta():
        return {"fields": [f.model_dump() | {"label": f.label} for f in FIELDS],
                "levels": LEVEL_LABELS, "temp_choice_fields": sorted(TEMP_CHOICES),
                "schema_version": SCHEMA_VERSION, "prompt_version": PROMPT_VERSION}

    @app.get("/api/documents")
    def documents(conn=Depends(get_conn)):
        return svc.list_documents(conn)

    @app.get("/api/documents/{document_id}")
    def document(document_id: int, conn=Depends(get_conn)):
        return call(svc.get_document, conn, document_id)

    @app.post("/api/documents/upload")
    def upload(file: UploadFile = File(...), title: str | None = Form(None)):
        """Save the paper and convert it to text locally. No API call: extraction is started separately.
        Sync endpoint with its own connection: PDF conversion is CPU work, and an SQLite connection
        must stay in the thread that made it."""
        data = file.file.read()
        title = (title or "").strip() or Path(file.filename or "paper").stem
        conn = connect(db_path)
        try:
            path = save_upload(papers_dir, file.filename or "", data, title)
            return import_file(conn, path, title, load_settings().max_doc_chars)
        except ImportError_ as e:
            raise HTTPException(400, str(e))
        finally:
            conn.close()

    @app.post("/api/documents/{document_id}/extract")
    def start_extract(document_id: int, conn=Depends(get_conn)):
        doc = call(svc.get_document, conn, document_id)
        try:
            return jobs.start(document_id, doc["title"])
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/jobs/current")
    def current_job():
        return jobs.current()

    @app.get("/api/records")
    def records(status: Literal["pending", "approved", "rejected", "needs_resolution"] | None = None,
                document_id: int | None = None, flagged: bool | None = None,
                kind: Literal["source", "condition", "observation"] | None = None, conn=Depends(get_conn)):
        return svc.list_records(conn, status=status, document_id=document_id, flagged=flagged, kind=kind)

    @app.get("/api/records/{record_id}")
    def record(record_id: int, conn=Depends(get_conn)):
        return call(svc.get_record, conn, record_id)

    @app.post("/api/records/{record_id}/approve")
    def approve(record_id: int, body: ApproveIn, conn=Depends(get_conn)):
        return call(svc.approve, conn, record_id, body.reviewer, body.note)

    @app.post("/api/records/{record_id}/reject")
    def reject(record_id: int, body: ReasonIn, conn=Depends(get_conn)):
        return call(svc.reject, conn, record_id, body.reviewer, body.reason)

    @app.post("/api/records/{record_id}/flag")
    def flag(record_id: int, body: ReasonIn, conn=Depends(get_conn)):
        return call(svc.flag_for_resolution, conn, record_id, body.reviewer, body.reason)

    @app.post("/api/records/{record_id}/reopen")
    def reopen(record_id: int, body: ReasonIn, conn=Depends(get_conn)):
        return call(svc.reopen, conn, record_id, body.reviewer, body.reason)

    @app.post("/api/records/{record_id}/edit")
    def edit(record_id: int, body: EditIn, conn=Depends(get_conn)):
        return call(svc.edit_field, conn, record_id, body.reviewer, body.field, body.reason,
                    value=body.value, quote=body.quote)

    def _download_name(conn, scope: str, document_id: int | None, ext: str) -> dict:
        title = "all_documents"
        if document_id is not None:
            title = call(svc.get_document, conn, document_id)["title"]
        name = f"{title}_{scope}.{ext}"
        ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
        return {"Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{urlquote(name)}'}

    Scope = Literal["approved", "all"]

    @app.get("/api/export.csv")
    def export_csv(scope: Scope = "approved", document_id: int | None = None, conn=Depends(get_conn)):
        headers = _download_name(conn, scope, document_id, "csv")
        return Response(export.to_csv_bytes(conn, scope, document_id), media_type="text/csv; charset=utf-8",
                        headers=headers)

    @app.get("/api/export.xlsx")
    def export_xlsx(scope: Scope = "approved", document_id: int | None = None, conn=Depends(get_conn)):
        headers = _download_name(conn, scope, document_id, "xlsx")
        return Response(export.to_xlsx_bytes(conn, scope, document_id),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers=headers)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
