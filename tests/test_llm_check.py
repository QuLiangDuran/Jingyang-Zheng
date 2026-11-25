"""Pre-flight check that the configured model really reads PDF pages as images."""
import pymupdf

from audit import llm_check
from audit.llm import ReplayClient


def test_probe_pdf_has_the_code_only_as_an_image(tmp_path):
    p = llm_check.make_probe_pdf(tmp_path / "probe.pdf", "KX7Q4M")
    doc = pymupdf.open(p)
    assert doc.page_count == 1
    assert doc[0].get_text().strip() == ""          # no text layer: only vision can read it
    assert doc[0].get_images()


class Answering(ReplayClient):
    def __init__(self, answer, supports_pdf=True):
        super().__init__([answer])
        self.supports_pdf = supports_pdf
        self.pdf_input = "inline" if supports_pdf else "off"


def test_check_passes_when_the_model_reads_the_code():
    r = llm_check.check_pdf_vision(Answering("The code is KX7Q4M."), code="KX7Q4M")
    assert r.ok and "KX7Q4M" in r.answer


def test_check_fails_when_the_model_cannot_read_it():
    r = llm_check.check_pdf_vision(Answering("I cannot see any code."), code="KX7Q4M")
    assert not r.ok


def test_check_explains_when_pdf_input_is_off():
    r = llm_check.check_pdf_vision(Answering("x", supports_pdf=False), code="KX7Q4M")
    assert not r.ok and "LLM_PDF_INPUT" in r.message


def test_check_reports_an_api_error():
    class Broken(Answering):
        def complete(self, *a, **k):
            raise RuntimeError("400 unsupported content type")
    r = llm_check.check_pdf_vision(Broken("x"), code="KX7Q4M")
    assert not r.ok and "unsupported content type" in r.message
