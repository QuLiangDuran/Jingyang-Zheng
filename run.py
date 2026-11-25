"""One-file launcher. Works from PowerShell, cmd or Git Bash:

    python run.py                      start the review page and open the browser
    python run.py add papers/x.pdf     import + AI-extract one or more papers (.pdf or .txt), then start the page
    python run.py add papers/x.txt --pdf papers/x.pdf --title 2025_YUAN
    python run.py reparse 3            re-parse document 3's failed AI answer without calling the API
    python run.py recheck 3            recompute document 3's flags with the current rules (no API call)
    python run.py recall 1 2           result sentences that no observation quotes (recall check, no API call)
    python run.py delete 1 2           delete documents 1 and 2 with their AI answers (refused once reviewed)
    python run.py reset                delete all documents and records (asks first; --yes to skip)
    python run.py check                check that the model reads attached PDF pages as images (tiny API call)
    python run.py test                 run the test suite
    python run.py demo                 load the fictional demo paper (no API key needed)

It always runs with the project's .venv Python, even if you type `python` outside the venv.
"""
import argparse
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8000


def _venv_python() -> Path | None:
    for p in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if p.is_file():
            return p
    return None


def _ensure_venv() -> None:
    """Re-run this script with the .venv interpreter if we are not already in it."""
    venv = _venv_python()
    if venv and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve() and not os.environ.get("RUN_PY_CHILD"):
        env = dict(os.environ, RUN_PY_CHILD="1")
        sys.exit(subprocess.call([str(venv), str(Path(__file__).resolve()), *sys.argv[1:]], env=env, cwd=ROOT))


def _audit(*args: str) -> int:
    return subprocess.call([sys.executable, "-m", "audit", *args], cwd=ROOT)


def _port_in_use() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def serve() -> int:
    url = f"http://{HOST}:{PORT}"
    if _port_in_use():
        print(f"Review page is already running: {url}  (refresh the browser to see new papers)")
        webbrowser.open(url)
        return 0
    print(f"Starting review page at {url}  -- press Ctrl+C to stop")
    proc = subprocess.Popen([sys.executable, "-m", "audit", "serve", "--host", HOST, "--port", str(PORT)], cwd=ROOT)
    for _ in range(40):  # wait up to ~10 s for the server
        if _port_in_use():
            webbrowser.open(url)
            break
        if proc.poll() is not None:
            return proc.returncode
        time.sleep(0.25)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


def add(paths: list[str], pdf: str | None, title: str | None) -> int:
    if (pdf or title) and len(paths) > 1:
        sys.exit("--pdf and --title only work with one paper at a time.")
    for p in paths:
        if not Path(p).is_file():
            sys.exit(f"File not found: {p}")
    if _pdf_input() != "off":  # make sure the model really sees the pages before paying for a paper
        print("Checking that the model reads PDF pages as images ...")
        if _audit("check-llm") != 0:
            sys.exit("PDF visual check failed; nothing was imported. Fix .env (or set LLM_PDF_INPUT=off "
                     "for text only), then run add again.")
    for p in paths:
        args = ["ingest", p, "--title", title or Path(p).stem]
        if pdf:
            args += ["--pdf", pdf]
        print(f"\n=== {p} ===")
        if _audit(*args) != 0:
            print(f"Import or extraction of {p} failed; see the message above.")
    return serve()


def _pdf_input() -> str:
    sys.path.insert(0, str(ROOT))
    from audit.config import load_settings
    return load_settings().llm_pdf_input


def reset(yes: bool) -> int:
    sys.path.insert(0, str(ROOT))
    from audit.config import load_settings
    db = load_settings().db_path
    if not db.exists():
        print(f"Nothing to delete: {db} does not exist.")
        return 0
    if not yes and input(f"Delete {db} with ALL documents, AI answers and review history? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1
    try:
        for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"), db.with_name(db.name + "-journal")):
            p.unlink(missing_ok=True)
    except PermissionError:
        sys.exit("The database is in use. Stop the review page (Ctrl+C in its window), then run reset again.")
    print(f"Deleted {db}. A fresh empty database is created on the next run.")
    if _port_in_use():
        print("Note: a review page is still running; restart it so it does not show stale data.")
    return 0


def main() -> int:
    _ensure_venv()
    p = argparse.ArgumentParser(prog="python run.py", description="Start the literature extraction audit tool.")
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("add", help="import + extract papers, then open the review page")
    a.add_argument("paths", nargs="+")
    a.add_argument("--pdf", help="original PDF when importing a .txt")
    a.add_argument("--title", help="document title, e.g. 2025_YUAN (default: file name)")
    r = sub.add_parser("reset", help="delete all documents and records")
    r.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    us = sub.add_parser("usage", help="token report: cache hits, reasoning, cumulative vs saved output")
    us.add_argument("document_id", nargs="?")
    rp = sub.add_parser("reparse", help="re-parse a document's failed AI answer (no API call)")
    rp.add_argument("document_id")
    rc = sub.add_parser("recheck", help="recompute flags with the current rules (no API call)")
    rc.add_argument("document_ids", nargs="+")
    rl = sub.add_parser("recall", help="result sentences no observation quotes (no API call)")
    rl.add_argument("document_ids", nargs="+")
    rl.add_argument("--all", action="store_true")
    d = sub.add_parser("delete", help="delete documents by id (refused once any record was reviewed)")
    d.add_argument("document_ids", nargs="+")
    sub.add_parser("check", help="check that the model reads attached PDF pages as images (one tiny API call)")
    sub.add_parser("test", help="run pytest")
    sub.add_parser("demo", help="load the fictional demo paper, then open the review page")
    sub.add_parser("serve", help="start the review page (same as no command)")
    args = p.parse_args()

    if args.cmd == "add":
        return add(args.paths, args.pdf, args.title)
    if args.cmd == "reset":
        return reset(args.yes)
    if args.cmd == "reparse":
        return _audit("reparse", args.document_id)
    if args.cmd == "usage":
        return _audit("usage", *([args.document_id] if args.document_id else []))
    if args.cmd == "recheck":
        return _audit("recheck", *args.document_ids)
    if args.cmd == "recall":
        return _audit("recall", *args.document_ids, *(["--all"] if args.all else []))
    if args.cmd == "delete":
        rc = _audit("delete-doc", *args.document_ids)
        if _port_in_use():
            print("Refresh the review page to see the change.")
        return rc
    if args.cmd == "check":
        return _audit("check-llm")
    if args.cmd == "test":
        return subprocess.call([sys.executable, "-m", "pytest", "-q"], cwd=ROOT)
    if args.cmd == "demo":
        _audit("demo")
        return serve()
    return serve()


if __name__ == "__main__":
    sys.exit(main())
