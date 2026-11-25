"""Command line: python -m audit <command>"""
import argparse
import json
from pathlib import Path

from . import export
from .config import PROJECT_ROOT, load_settings
from .db import connect
from .documents import load_text
from .extract import add_document, extract_document
from .llm import OpenAICompatClient, ReplayClient

SAMPLES = PROJECT_ROOT / "samples"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m audit")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create the SQLite database")

    a = sub.add_parser("ingest", help="import a plain-text paper and run AI extraction")
    a.add_argument("path")
    a.add_argument("--title")
    a.add_argument("--no-extract", action="store_true", help="only import, do not call the LLM")

    a = sub.add_parser("extract", help="run AI extraction on an imported document")
    a.add_argument("document_id", type=int)

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

    conn = connect(s.db_path)
    if args.cmd == "init-db":
        print(f"Database ready: {s.db_path}")
    elif args.cmd == "ingest":
        path = Path(args.path)
        doc_id = add_document(conn, args.title or path.stem, load_text(path), s.max_doc_chars, str(path))
        print(f"Imported document {doc_id}")
        if not args.no_extract:
            _report(extract_document(conn, doc_id, OpenAICompatClient.from_settings(s)))
    elif args.cmd == "extract":
        _report(extract_document(conn, args.document_id, OpenAICompatClient.from_settings(s)))
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


def _report(summary) -> None:
    print(f"Document {summary.document_id}: {summary.runs} segment(s), "
          f"{summary.records} proposed record(s), {summary.failed_runs} failed call(s)")
