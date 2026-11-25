"""FastAPI app: thin HTTP wrapper around review_service and export."""
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import export, review_service as svc
from .config import load_settings
from .db import connect
from .extract import PROMPT_VERSION
from .fields import FIELDS, LEVEL_LABELS, SCHEMA_VERSION, TEMP_CHOICES

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


def create_app(db_path: str | Path | None = None) -> FastAPI:
    db_path = db_path or load_settings().db_path
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

    @app.get("/api/export.csv")
    def export_csv(conn=Depends(get_conn)):
        return Response(export.to_csv_bytes(conn), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="approved.csv"'})

    @app.get("/api/export.xlsx")
    def export_xlsx(conn=Depends(get_conn)):
        return Response(export.to_xlsx_bytes(conn),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="approved.xlsx"'})

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
