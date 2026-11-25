"""Pre-flight check: does the configured model really read the attached PDF as page images?

A one-page PDF is generated whose only content is a random code drawn as a picture (no text
layer). The model can only answer with that code if it looks at the page, so a pass proves native
PDF visual input works end to end (request format, provider, model) before a paper is extracted.
"""
import random
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf

ALPHABET = "ACDEFHJKMNPRTWXY3479"  # no look-alikes such as O/0, I/1, S/5


@dataclass
class CheckResult:
    ok: bool
    message: str
    answer: str | None = None
    usage: dict | None = None


def make_probe_pdf(path: str | Path, code: str) -> Path:
    """A one-page PDF showing `code` as an image only."""
    src = pymupdf.open()
    page = src.new_page(width=300, height=120)
    page.insert_text((30, 75), code, fontsize=48)
    png = page.get_pixmap(dpi=150).tobytes("png")
    out = pymupdf.open()
    out.new_page(width=300, height=120).insert_image(pymupdf.Rect(0, 0, 300, 120), stream=png)
    path = Path(path)
    out.save(path)
    return path


def check_pdf_vision(llm, code: str | None = None) -> CheckResult:
    if not getattr(llm, "supports_pdf", False):
        return CheckResult(False, f"LLM_PDF_INPUT={getattr(llm, 'pdf_input', 'off')}：没有开启 PDF 输入，"
                                  "模型只会收到提取出的文字。千问请在 .env 里设 LLM_PDF_INPUT=images（或 inline）。")
    code = code or "".join(random.choice(ALPHABET) for _ in range(6))
    with tempfile.TemporaryDirectory() as d:
        pdf = make_probe_pdf(Path(d) / "vision_check.pdf", code)
        try:
            answer = llm.complete("You read documents carefully.",
                                  "The attached PDF page shows a 6-character code. Reply with the code only.",
                                  pdf_path=pdf)
        except Exception as e:
            return CheckResult(False, f"调用失败（{type(e).__name__}: {e}）。这个模型或接口可能不接受 PDF 文件输入。")
        finally:
            if hasattr(llm, "cleanup"):
                llm.cleanup()
    usage = getattr(llm, "last_usage", None)
    norm = "".join(ch for ch in (answer or "").upper() if ch.isalnum())
    if code in norm:
        return CheckResult(True, f"通过：模型从 PDF 页面图像里读出了 {code}。", answer, usage)
    return CheckResult(False, f"未通过：页面上的编码是 {code}，模型回答 {answer!r}。"
                              "模型可能没有看页面图像（只读了文字层），读图结果不可信。", answer, usage)
