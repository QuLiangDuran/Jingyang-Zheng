"""Command line: python -m audit <command>"""
import argparse
import json
import sys
from pathlib import Path

from . import export
from .config import PROJECT_ROOT, load_settings
from .db import connect
from .documents import load_document, load_text
from .extract import add_document, delete_document, extract_document, reparse_document
from .llm import OpenAICompatClient, ReplayClient
from .review_service import refresh_document

SAMPLES = PROJECT_ROOT / "samples"


def main(argv: list[str] | None = None) -> None:
    # Windows consoles are often GBK: print paper text (−, μ, ∼ ...) without crashing
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(prog="python -m audit")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create the SQLite database")

    a = sub.add_parser("ingest", help="import a paper (.pdf or plain text) and run AI extraction")
    a.add_argument("path")
    a.add_argument("--title")
    a.add_argument("--no-extract", action="store_true", help="only import, do not call the LLM")
    a.add_argument("--pdf", help="original PDF of a .txt paper, sent to the model with the text "
                                 "(a .pdf path is attached automatically)")

    a = sub.add_parser("pdf2txt", help="convert a PDF to the text the AI will see, to check it first")
    a.add_argument("pdf")
    a.add_argument("out", nargs="?", help="output .txt (default: next to the PDF)")

    a = sub.add_parser("extract", help="run AI extraction on an imported document")
    a.add_argument("document_id", type=int)

    a = sub.add_parser("usage", help="token report: input, cache hits, cumulative API output, reasoning, saved effective output")
    a.add_argument("document_id", type=int, nargs="?")

    a = sub.add_parser("reparse", help="re-parse failed runs from their stored answers (repairs continuation seams; no API call)")
    a.add_argument("document_id", type=int)

    a = sub.add_parser("recheck", help="recompute the flags of documents with the current rules (no API call; content unchanged)")
    a.add_argument("document_id", type=int, nargs="+")

    a = sub.add_parser("recall", help="result sentences (Results/Discussion) that no observation quotes (no API call)")
    a.add_argument("document_id", type=int, nargs="+")
    a.add_argument("--all", action="store_true", help="list every uncovered sentence, not only the first 30")

    a = sub.add_parser("delete-doc", help="delete a document with its runs and records (refused once reviewed)")
    a.add_argument("document_id", type=int, nargs="+")

    sub.add_parser("check-llm", help="check that the model reads attached PDF pages as images (one tiny API call)")

    sub.add_parser("demo", help="load the sample paper with canned AI responses (no API key needed)")

    a = sub.add_parser("serve", help="start the review web page")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8000)

    a = sub.add_parser("export", help="export approved records (.csv or .xlsx)")
    a.add_argument("out")

    args = p.parse_args(argv)
    s = load_settings()

    if args.cmd == "serve":
        import uvicorn
        from .api import create_app
        print(f"Open http://{args.host}:{args.port}  (db: {s.db_path})")
        uvicorn.run(create_app(s.db_path), host=args.host, port=args.port)
        return

    if args.cmd == "pdf2txt":
        src = Path(args.pdf)
        out = Path(args.out) if args.out else src.with_suffix(".txt")
        text = load_document(src)
        out.write_text(text, encoding="utf-8")
        print(f"Wrote {out} ({len(text):,} characters). Check it, fix it if needed, then ingest the .txt.")
        return

    if args.cmd == "check-llm":
        from .llm_check import check_pdf_vision
        llm = OpenAICompatClient.from_settings(s)
        print(f"Model {s.llm_model} at {s.llm_base_url}, LLM_PDF_INPUT={s.llm_pdf_input}")
        r = check_pdf_vision(llm)
        print(r.message)
        if r.usage:
            print(f"  tokens: input {r.usage.get('prompt_tokens')}, output {r.usage.get('completion_tokens')}")
        raise SystemExit(0 if r.ok else 1)

    conn = connect(s.db_path)
    if args.cmd == "init-db":
        print(f"Database ready: {s.db_path}")
    elif args.cmd == "ingest":
        path = Path(args.path)
        pdf = Path(args.pdf) if args.pdf else (path if path.suffix.lower() == ".pdf" else None)
        if pdf and not pdf.is_file():
            raise SystemExit(f"PDF not found: {pdf}")
        # source_path = the original PDF when there is one, so extraction can attach it
        source = str((pdf or path).resolve())
        doc_id = add_document(conn, args.title or path.stem, load_document(path), s.max_doc_chars, source)
        print(f"Imported document {doc_id}")
        if not args.no_extract:
            _report(extract_document(conn, doc_id, OpenAICompatClient.from_settings(s)))
    elif args.cmd == "extract":
        _report(extract_document(conn, args.document_id, OpenAICompatClient.from_settings(s)))
    elif args.cmd == "reparse":
        _report(reparse_document(conn, args.document_id))
    elif args.cmd == "usage":
        from .usage_report import format_report, report
        print(format_report(report(conn, args.document_id)))
    elif args.cmd == "recheck":
        for doc_id in args.document_id:
            print(f"Document {doc_id}: {_flag_counts(conn, doc_id)} before")
            with conn:
                refresh_document(conn, doc_id)
            print(f"Document {doc_id}: {_flag_counts(conn, doc_id)} after")
    elif args.cmd == "recall":
        from .fields import RecordContent
        from collections import Counter
        from .recall import coverage, observation_quotes, skipped_sentences
        for doc_id in args.document_id:
            row = conn.execute("SELECT title, text FROM documents WHERE id=?", (doc_id,)).fetchone()
            if row is None:
                print(f"Document {doc_id}: not found")
                continue
            obs = [RecordContent.model_validate_json(r["current_json"]) for r in conn.execute(
                "SELECT current_json FROM records WHERE document_id=? AND kind='observation' AND status != 'rejected'",
                (doc_id,))]
            total, unc = coverage(row["text"], observation_quotes(obs))
            pct = 100 * (total - len(unc)) / total if total else 100
            skipped = Counter(r for _, _, r in skipped_sentences(row["text"]))
            print(f"Document {doc_id} {row['title']}: {total} result sentence(s), {total - len(unc)} covered "
                  f"({pct:.0f}%), {len(unc)} uncovered; {len(obs)} observation(s)")
            print("  left out: " + (", ".join(f"{n} {r}" for r, n in sorted(skipped.items())) or "none")
                  + " (captions, headings/axis labels, setting-only sentences)")
            for a, b in (unc if args.all else unc[:30]):
                print("  -", " ".join(row["text"][a:b].split())[:200])
            if not args.all and len(unc) > 30:
                print(f"  ... {len(unc) - 30} more (--all)")
    elif args.cmd == "delete-doc":
        for doc_id in args.document_id:
            try:
                c = delete_document(conn, doc_id)
            except ValueError as e:
                print(f"Document {doc_id}: not deleted ({e})")
                continue
            print(f"Deleted document {doc_id}: {c['records']} record(s), {c['extraction_runs']} run(s), "
                  f"{c['segments']} segment(s)")
    elif args.cmd == "demo":
        text = load_text(SAMPLES / "sample_paper.txt")
        responses = json.loads((SAMPLES / "sample_responses.json").read_text(encoding="utf-8"))
        doc_id = add_document(conn, "示例论文（虚构微藻论文）", text, s.max_doc_chars, "samples/sample_paper.txt")
        _report(extract_document(conn, doc_id, AnchoredReplayClient(responses)))
    elif args.cmd == "export":
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = export.to_xlsx_bytes(conn) if out.suffix.lower() == ".xlsx" else export.to_csv_bytes(conn)
        out.write_bytes(data)
        print(f"Wrote {out}")
    conn.close()


class AnchoredReplayClient(ReplayClient):
    """Demo client: answers each segment with the canned entries whose `anchor` it contains."""

    def __init__(self, entries: list[dict]):
        super().__init__([], model="demo-replay")
        self.entries = entries

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        hits = [e for e in self.entries if e["anchor"] in user]
        if any("raw" in e for e in hits):  # a deliberately malformed answer
            return next(e["raw"] for e in hits if "raw" in e)
        out: dict = {}
        for e in hits:
            resp = e["response"]
            if resp.get("source") is not None:
                out["source"] = resp["source"]
            for k in ("conditions", "observations"):
                if k in resp:
                    out.setdefault(k, []).extend(resp[k])
        return json.dumps(out, ensure_ascii=False)


def _flag_counts(conn, document_id: int) -> str:
    n = {"error": 0, "warning": 0}
    for (fj,) in conn.execute("SELECT flags_json FROM records WHERE document_id=?", (document_id,)):
        for f in json.loads(fj):
            n[f["level"]] = n.get(f["level"], 0) + 1
    return f"{n['error']} error(s), {n['warning']} warning(s)"


def _report(summary) -> None:
    print(f"Document {summary.document_id}: {summary.runs} segment(s), "
          f"{summary.records} proposed record(s), {summary.failed_runs} failed call(s)")
    if summary.calls:
        print(f"  API calls: {summary.calls} (continuations included), output tokens: {summary.completion_tokens:,}")
    for e in summary.errors:
        print(f"  error: {e}")
