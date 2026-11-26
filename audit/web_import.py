"""Import from the review page: save an uploaded paper, convert it locally, extract on request.

Uploading never calls the model: the PDF is converted to text here (documents.py) and stored as a
document, so the reviewer can check the text first. Extraction is a separate, explicit step that
runs in a background thread (one at a time), because it takes minutes and costs API tokens.
"""
import hashlib
import re
import sqlite3
import threading
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .db import connect, now
from .documents import load_document
from .extract import add_document, extract_document

ALLOWED = (".pdf", ".txt")
MAX_BYTES = 60 * 1024 * 1024


class ImportError_(ValueError):
    """The upload cannot be imported (wrong type, too big, scanned PDF ...)."""


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._") or "paper"
    return stem[:120]


def save_upload(papers_dir: Path, filename: str, data: bytes, title: str | None) -> Path:
    """Write the upload to papers/<title or file name>.<ext>. A file with the same name and the same
    bytes is reused; a different file with that name gets -2, -3 ... (nothing is overwritten)."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise ImportError_(f"只能导入 PDF 或 TXT 文件（收到 {ext or '无扩展名'}）")
    if not data:
        raise ImportError_("文件是空的")
    if len(data) > MAX_BYTES:
        raise ImportError_(f"文件超过 {MAX_BYTES // 1024 // 1024} MB")
    papers_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(title or Path(filename).stem)
    digest = hashlib.sha256(data).hexdigest()
    for i in range(1, 1000):
        p = papers_dir / (f"{stem}{ext}" if i == 1 else f"{stem}-{i}{ext}")
        if not p.exists():
            p.write_bytes(data)
            return p
        if hashlib.sha256(p.read_bytes()).hexdigest() == digest:
            return p
    raise ImportError_("同名文件太多")


def import_file(conn: sqlite3.Connection, path: Path, title: str, max_chars: int) -> dict:
    """Convert locally (no API call) and add the document. Returns a short report for the page."""
    try:
        text = load_document(path)
    except Exception as e:  # scanned PDF, unreadable file, missing PyMuPDF ...
        raise ImportError_(f"转换失败：{e}") from e
    if not text.strip():
        raise ImportError_("没有提取到文字（可能是扫描版 PDF）")
    pages = None
    if path.suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(path) as d:
            pages = d.page_count
    doc_id = add_document(conn, title, text, max_chars, str(path.resolve()))
    return {"id": doc_id, "title": title, "file": path.name, "pages": pages, "chars": len(text),
            "preview": text[:800]}


@dataclass
class Job:
    document_id: int
    title: str
    status: str = "running"          # running | done | error
    step: str = "starting"
    message: str = ""
    started_at: str = field(default_factory=now)
    finished_at: str | None = None
    summary: dict | None = None


class ExtractionJobs:
    """One extraction at a time; the page polls `current()`."""

    def __init__(self, db_path, llm_factory):
        self.db_path = db_path
        self.llm_factory = llm_factory
        self._lock = threading.Lock()
        self._job: Job | None = None

    def current(self) -> dict | None:
        with self._lock:
            return asdict(self._job) if self._job else None

    def start(self, document_id: int, title: str) -> dict:
        with self._lock:
            if self._job and self._job.status == "running":
                raise RuntimeError(f"正在提取「{self._job.title}」，请等它结束")
            self._job = Job(document_id, title)
        threading.Thread(target=self._run, args=(self._job,), daemon=True).start()
        return self.current()

    def _set(self, job: Job, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(job, k, v)

    def _run(self, job: Job) -> None:
        conn = connect(self.db_path)
        try:
            llm = self.llm_factory()
            if getattr(llm, "supports_pdf", False):
                from .extract import _pdf_for
                src = conn.execute("SELECT source_path FROM documents WHERE id=?", (job.document_id,)).fetchone()
                if src and _pdf_for(src["source_path"]):
                    self._set(job, step="读图自检")
                    from .llm_check import check_pdf_vision
                    r = check_pdf_vision(llm)
                    if not r.ok:
                        self._set(job, status="error", step="读图自检", message=r.message, finished_at=now())
                        return
            self._set(job, step="调用模型提取（约 1–3 分钟）")
            s = extract_document(conn, job.document_id, llm)
            summary = {"records": s.records, "runs": s.runs, "failed_runs": s.failed_runs, "calls": s.calls,
                       "completion_tokens": s.completion_tokens, "errors": [str(e) for e in s.errors]}
            msg = f"完成：{s.records} 条记录，{s.calls} 次 API 调用，输出 {s.completion_tokens:,} tokens"
            if s.failed_runs:
                msg = f"有 {s.failed_runs} 次调用失败：" + "；".join(str(e) for e in s.errors)[:500]
            self._set(job, status="done", step="完成", message=msg, summary=summary, finished_at=now())
        except Exception as e:
            traceback.print_exc()
            self._set(job, status="error", message=f"{type(e).__name__}: {e}"[:500], finished_at=now())
        finally:
            conn.close()
