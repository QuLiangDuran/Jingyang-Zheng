"""Export approved observations as one flat row each, in Extraction Template column order.

A row = observation + its condition + its paper (source). It is exported only when
all three records are approved. Empty cells are filled with "Missing" / "N/A"
according to each field's missing policy; electrical fields on non-electrical
rows are "N/A"; "later" fields stay blank.
"""
import csv
import io
import sqlite3

from .fields import EXPORT_FIELDS, FIELDS, RecordContent
from .review_service import DocView, reviewer_notes

QUOTE_FIELDS = [f for f in FIELDS if f.filled_by == "ai" and f.needs_quote]
EXTRA_COLUMNS = ["condition_source_locator", "observation_record_id", "condition_record_id", "source_record_id",
                 "document", "edited", "model", "prompt_version", "schema_version"]
SOURCE_SHEET_COLUMNS = ["source_record_id", "study_id", "source_id", "review_status", "doi", "article_title",
                        "first_author", "publication_year", "paper_type", "paper_verdict", "exclusion_reason",
                        "uncertain_reason", "is_electrical", "exported_rows", "reviewer_id", "reviewed_at"]


def export_columns() -> list[str]:
    return [f.name for f in EXPORT_FIELDS] + [f"{f.name}_quote" for f in QUOTE_FIELDS] + EXTRA_COLUMNS


def _fill(spec, value, electrical: bool):
    if value not in (None, ""):
        return value
    if spec.filled_by == "later":
        return None
    if spec.electrical_only and not electrical:
        return "N/A"
    return spec.missing or None


def _rows_for_document(conn: sqlite3.Connection, document_id: int, title: str) -> tuple[list[dict], list[dict]]:
    doc = DocView(conn, document_id)
    ids = doc.ids(document_id)
    rid = ids["records"]
    by_id = {r["id"]: r for r in doc.rows}
    runs = {r["id"]: r for r in conn.execute("SELECT id, model, prompt_version, schema_version FROM extraction_runs "
                                             "WHERE document_id=?", (document_id,))}
    rows, exported_per_source = [], {}
    for o in doc.by_kind["observation"]:
        c = by_id.get(doc.parent[o["id"]])
        s = by_id.get(doc.parent[c["id"]]) if c else None
        if not (o["status"] == "approved" and c and c["status"] == "approved" and s and s["status"] == "approved"):
            continue
        exported_per_source[s["id"]] = exported_per_source.get(s["id"], 0) + 1
        content = {"source": doc.content[s["id"]], "condition": doc.content[c["id"]],
                   "observation": doc.content[o["id"]]}
        electrical = doc.electrical[o["id"]]
        ctrl, comp_c, comp_o = (doc.resolve(c["id"], "control_condition_ref"),
                                doc.resolve(o["id"], "comparator_condition_ref"),
                                doc.resolve(o["id"], "comparator_observation_ref"))
        system = {
            "study_id": ids["study_id"], "source_id": rid[s["id"]]["system_id"], "parent_source_id": None,
            "condition_id": rid[c["id"]]["system_id"], "experiment_id": rid[c["id"]]["experiment_id"],
            "arm_id": rid[c["id"]]["arm_id"],
            "control_condition_id": rid[ctrl]["system_id"] if ctrl else None,
            "observation_id": rid[o["id"]]["system_id"],
            "comparator_condition_id": rid[comp_c]["system_id"] if comp_c else None,
            "comparator_observation_id": rid[comp_o]["system_id"] if comp_o else None,
            "review_status": o["status"], "reviewer_id": o["reviewer"], "reviewed_at": o["reviewed_at"],
            "reviewer_notes": reviewer_notes(conn, o["id"]), "is_electrical": electrical,
        }
        row: dict = {}
        for f in EXPORT_FIELDS:
            v = system.get(f.name) if f.filled_by in ("system_id", "reviewer", "derived") \
                else content[f.level].get(f.name) if f.filled_by == "ai" else None
            row[f.name] = _fill(f, v, electrical)
        for f in QUOTE_FIELDS:
            fv = content[f.level].fields.get(f.name)
            row[f"{f.name}_quote"] = fv.quote if fv else None
        run = runs[o["run_id"]]
        row.update(
            condition_source_locator=content["condition"].get("condition_source_locator"),
            observation_record_id=o["id"], condition_record_id=c["id"], source_record_id=s["id"],
            document=title,
            edited=any(by_id[x]["current_json"] != by_id[x]["proposed_json"] for x in (o["id"], c["id"], s["id"])),
            model=run["model"], prompt_version=run["prompt_version"], schema_version=run["schema_version"])
        rows.append(row)

    sources = []
    for s in doc.by_kind["source"]:
        sc: RecordContent = doc.content[s["id"]]
        sources.append({
            "source_record_id": s["id"], "study_id": ids["study_id"], "source_id": rid[s["id"]]["system_id"],
            "review_status": s["status"], **{k: sc.get(k) for k in SOURCE_SHEET_COLUMNS if sc.get(k) is not None},
            "is_electrical": doc.electrical[s["id"]], "exported_rows": exported_per_source.get(s["id"], 0),
            "reviewer_id": s["reviewer"], "reviewed_at": s["reviewed_at"],
        })
    return rows, sources


def _collect(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    rows, sources = [], []
    for d in conn.execute("SELECT id, title FROM documents ORDER BY id").fetchall():
        r, s = _rows_for_document(conn, d["id"], d["title"])
        rows += r
        sources += s
    return rows, sources


def export_rows(conn: sqlite3.Connection) -> list[dict]:
    return _collect(conn)[0]


def to_csv_bytes(conn: sqlite3.Connection) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=export_columns())
    w.writeheader()
    w.writerows(export_rows(conn))
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel reads non-ASCII (µ, °C, Chinese) correctly


def to_xlsx_bytes(conn: sqlite3.Connection) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    rows, sources = _collect(conn)
    wb = Workbook()
    for ws, cols, data in ((wb.active, export_columns(), rows), (wb.create_sheet(), SOURCE_SHEET_COLUMNS, sources)):
        ws.append(cols)
        for c in ws[1]:
            c.font = Font(bold=True)
        for row in data:
            ws.append([row.get(c) for c in cols])
        ws.freeze_panes = "A2"
    wb.worksheets[0].title = "observations"
    wb.worksheets[1].title = "sources"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
