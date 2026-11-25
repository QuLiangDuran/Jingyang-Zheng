"""Document input and segmentation."""
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Segment:
    idx: int
    start: int  # char offset in document text (inclusive)
    end: int    # char offset (exclusive)


def load_text(path: str | Path) -> str:
    text = Path(path).read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def load_pdf(path: str | Path) -> str:
    """Extract the text layer of a PDF and clean it into paragraphs.

    Uses PyMuPDF (``pip install pymupdf``) when installed -- best reading order for two-column
    papers -- and falls back to pypdf. Scanned PDFs without a text layer are refused (no OCR).
    Figures are not read: values that only appear in a figure are not in the text.
    """
    pages = _pdf_pages(Path(path))
    text = clean_pdf_pages(pages)
    if len(re.sub(r"\s", "", text)) < 200:
        raise ValueError(f"{path}: almost no text found -- probably a scanned PDF (OCR is not supported).")
    return text


def load_document(path: str | Path) -> str:
    """Load a paper as text: .pdf through load_pdf, anything else as UTF-8 text."""
    return load_pdf(path) if Path(path).suffix.lower() == ".pdf" else load_text(path)


def _pdf_pages(path: Path) -> list[list[str]]:
    """One list of text blocks (paragraph candidates) per page."""
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24 (the old name "fitz" is deprecated)
    except ImportError:
        try:
            import fitz
        except ImportError:
            fitz = None
    if fitz is not None:
        with fitz.open(path) as doc:
            # blocks: (x0, y0, x1, y1, text, block_no, block_type); type 0 = text.
            # Content-stream order is kept on purpose: sorting by position interleaves columns.
            return [[b[4] for b in page.get_text("blocks") if b[6] == 0] for page in doc]
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ImportError("Reading PDFs needs PyMuPDF: run  .venv\\Scripts\\pip install pymupdf") from e
    pages = []
    for page in PdfReader(str(path)).pages:
        raw = page.extract_text() or ""
        pages.append(_pypdf_blocks(raw))
    return pages


def _pypdf_blocks(raw: str) -> list[str]:
    """pypdf gives lines, not paragraphs: start a new block after a blank line, or after a line
    that ends a sentence and is clearly shorter than the full line width."""
    lines = [ln.rstrip() for ln in raw.replace("\r", "").split("\n")]
    width = max((len(ln) for ln in lines), default=0)
    blocks, cur = [], []
    for ln in lines:
        if not ln.strip():
            if cur:
                blocks.append("\n".join(cur)); cur = []
            continue
        cur.append(ln)
        if re.search(r"[.:!?]$", ln) and len(ln) < 0.7 * width:
            blocks.append("\n".join(cur)); cur = []
    if cur:
        blocks.append("\n".join(cur))
    return blocks


_LIGATURES = {"\u00a0": " ", "\u202f": " ", "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
              "\u00ad": "", "\u200b": ""}


def _norm_line(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())


EDGE = 6


def _edge(page_lines: list) -> int:
    """How many lines at each end of a page may be header/footer: EDGE, but at most a third of a
    short page, so a body line that repeats on every page is not taken for a header."""
    return max(1, min(EDGE, len(page_lines) // 3))


def clean_pdf_pages(pages: list[list[str]]) -> str:
    """Turn per-page text blocks into plain text with one paragraph per blank-line block.

    - removes running headers/footers (a short line among the first or last EDGE lines of a page
      that repeats there on at least half of the pages, digits ignored) and bare page numbers
    - joins hyphenated line breaks when a lowercase letter follows ("photo-\nsynthesis")
    - joins the remaining line breaks inside a block with a space
    - replaces ligatures (fi, fl ...) and soft hyphens; nothing else is changed, so quotes from
      this text match it exactly
    """
    for k, v in _LIGATURES.items():
        pages = [[b.replace(k, v) for b in blocks] for blocks in pages]
    # Header/footer candidates: only the first and last EDGE lines of each page. ACS puts journal
    # name, URL, "Research Article", DOI, citation and page number as the last six lines.
    counts: dict[str, int] = {}
    for blocks in pages:
        page_lines = [ln for b in blocks for ln in b.splitlines() if ln.strip()]
        k = _edge(page_lines)
        edge = page_lines[:k] + page_lines[-k:]
        seen = {_norm_line(ln) for ln in edge if len(ln.strip()) <= 120}
        for key in seen:
            counts[key] = counts.get(key, 0) + 1
    repeated = {k for k, n in counts.items() if len(pages) >= 3 and n >= max(3, len(pages) / 2)}

    paragraphs = []
    for blocks in pages:
        page_lines = [ln.strip() for b in blocks for ln in b.splitlines() if ln.strip()]
        k = _edge(page_lines)
        edges = set(page_lines[:k] + page_lines[-k:])
        numbers = set(page_lines[:2] + page_lines[-2:])  # bare page numbers: outermost lines only
        for b in blocks:
            lines = [ln.strip() for ln in b.splitlines()]
            lines = [ln for ln in lines if ln and (_norm_line(ln) not in repeated or ln not in edges)
                     # a bare number is a page number only at the top or bottom of a page; elsewhere
                     # it is data (PyMuPDF puts each table cell on its own line)
                     and not (ln in numbers and re.fullmatch(r"(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?", ln, re.I))]
            if not lines:
                continue
            para = lines[0]
            for ln in lines[1:]:
                if re.search(r"[A-Za-z]-$", para) and ln[:1].islower():
                    para = para[:-1] + ln
                else:
                    para = para + " " + ln
            # A sentence broken across pages stays in two paragraphs (the page's last block is often
            # a figure caption, so joining would glue body text onto it); quote matching collapses
            # whitespace, so a quote spanning the break still matches.
            paragraphs.append(re.sub(r"[ \t]+", " ", para))
    return "\n\n".join(p for p in _drop_repeats(paragraphs) if p) + "\n"


_DRAFT_HEADER = re.compile(r"\S+ \d{4}, \d+, x FOR PEER REVIEW \d+ of \d+ ?", re.I)
# A sentence ends at . ! ? that is not followed by a digit (so 0.845 stays whole); nothing is lost:
# the pieces always join back to the full paragraph.
_SENTENCE = re.compile(r"(?:[^.!?]|[.!?](?=\d))+(?:[.!?]+|$)|[.!?]+")


def _drop_repeats(paragraphs: list[str]) -> list[str]:
    """Some PDFs (e.g. MDPI) embed figures as draft pages that carry their own text layer, so text
    near a figure appears twice. Drop draft page headers, and drop a sentence of 40+ characters
    when the same sentence (spaces ignored) already appeared earlier in the document."""
    seen: set[str] = set()
    out = []
    for para in paragraphs:
        para = _DRAFT_HEADER.sub("", para)
        kept = []
        for sent in _SENTENCE.findall(para):
            key = re.sub(r"\s+", "", sent).lower()
            if len(key) >= 40:
                if key in seen:
                    continue
                seen.add(key)
            kept.append(sent)
        out.append(re.sub(r" {2,}", " ", "".join(kept)).strip())
    return out


def segment_text(text: str, max_chars: int) -> list[Segment]:
    """Split on blank lines into paragraphs, then merge neighbours up to max_chars.

    A single paragraph longer than max_chars becomes its own segment (never split
    mid-paragraph, so quotes stay intact).
    """
    paragraphs = [(m.start(), m.end()) for m in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\s*\Z)", text, re.S)]
    segments: list[Segment] = []
    cur_start = cur_end = None
    for p_start, p_end in paragraphs:
        if cur_start is None:
            cur_start, cur_end = p_start, p_end
        elif p_end - cur_start <= max_chars:
            cur_end = p_end
        else:
            segments.append(Segment(len(segments), cur_start, cur_end))
            cur_start, cur_end = p_start, p_end
    if cur_start is not None:
        segments.append(Segment(len(segments), cur_start, cur_end))
    return segments
