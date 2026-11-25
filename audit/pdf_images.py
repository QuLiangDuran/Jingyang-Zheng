"""Render PDF pages locally as PNG images for LLM_PDF_INPUT=images.

Instead of sending the whole PDF (which the provider turns into page images itself, about 3,700
tokens per page on Qwen), the pages are rendered here and sent as images next to the extracted
text. By default only pages with a figure or table are sent: the text already covers the rest.
Measured on Qwen (2022_KURUVINASHETTI Fig. 4): 72 dpi misread bar labels, 110 and 150 dpi read
them correctly (about 1,150 and 2,100 tokens per page).
"""
import re
from functools import lru_cache
from pathlib import Path

import pymupdf

PAGE_MODES = ("figures", "all")
CAPTION = re.compile(r"^\s*(fig\.?|figure|table|scheme)\s*S?\d+", re.I | re.M)
MIN_DRAWINGS = 200  # vector charts are hundreds of paths; ruled text pages have a few


def figure_pages(doc: pymupdf.Document) -> list[int]:
    """1-based numbers of pages with a figure or table caption, an embedded picture or a vector chart."""
    out = []
    for i, page in enumerate(doc):
        if CAPTION.search(page.get_text()) or page.get_images() or len(page.get_drawings()) >= MIN_DRAWINGS:
            out.append(i + 1)
    return out


def page_images(pdf_path: str | Path, dpi: int = 150, pages: str = "figures") -> list[tuple[int, bytes]]:
    """(page number, PNG bytes) for the selected pages."""
    p = Path(pdf_path)
    st = p.stat()
    return list(_render(str(p.resolve()), st.st_mtime_ns, st.st_size, dpi, pages))


@lru_cache(maxsize=8)  # a long paper is sent in chunks: render once
def _render(path: str, mtime: int, size: int, dpi: int, pages: str) -> tuple[tuple[int, bytes], ...]:
    if pages not in PAGE_MODES:
        raise ValueError(f"LLM_IMAGE_PAGES must be one of {', '.join(PAGE_MODES)}, not {pages!r}")
    doc = pymupdf.open(path)
    numbers = figure_pages(doc) if pages == "figures" else list(range(1, doc.page_count + 1))
    return tuple((n, doc[n - 1].get_pixmap(dpi=dpi).tobytes("png")) for n in numbers)
