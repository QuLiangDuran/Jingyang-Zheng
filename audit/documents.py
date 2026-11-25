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
    """Placeholder: PDF parsing is out of scope for v1."""
    raise NotImplementedError("PDF parsing is not implemented yet; provide plain text.")


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
