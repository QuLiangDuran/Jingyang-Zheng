"""LLM_PDF_INPUT=images (pages rendered locally, sent as images beside the text) and LLM_CACHE_CONTROL."""
import base64
from types import SimpleNamespace

import pymupdf
import pytest

from audit import extract
from audit.config import _pdf_mode, load_settings
from audit.llm import OpenAICompatClient
from audit.pdf_images import figure_pages, page_images


def _pdf(tmp_path):
    """Page 1: plain text. Page 2: a figure caption. Page 3: an embedded picture, no caption."""
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Introduction. Plain running text only.")
    doc.new_page().insert_text((72, 72), "Figure 2. Photocurrent of the BPV device.")
    pic = pymupdf.open()
    pic.new_page(width=50, height=50).draw_rect(pymupdf.Rect(5, 5, 45, 45), fill=(1, 0, 0))
    png = pic[0].get_pixmap(dpi=72).tobytes("png")
    doc.new_page().insert_image(pymupdf.Rect(72, 72, 272, 272), stream=png)
    p = tmp_path / "paper.pdf"
    doc.save(p)
    return p


def test_figure_pages_finds_captions_and_pictures(tmp_path):
    assert figure_pages(pymupdf.open(_pdf(tmp_path))) == [2, 3]


def test_page_images_renders_selected_pages(tmp_path):
    p = _pdf(tmp_path)
    figs = page_images(p, dpi=72, pages="figures")
    assert [n for n, _ in figs] == [2, 3]
    assert figs[0][1][:8] == b"\x89PNG\r\n\x1a\n"
    assert [n for n, _ in page_images(p, dpi=72, pages="all")] == [1, 2, 3]


class Fake:
    def __init__(self, answers=None):
        self.answers = list(answers or [('{"source": null, "conditions": [], "observations": []}', "stop")])
        self.messages = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))

    def _chat(self, model, temperature, messages, **kw):
        self.messages.append(messages)
        text, finish = self.answers.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def _client(fake, **kw):
    return OpenAICompatClient("http://x", "m", "k", client=fake, **kw)


def test_images_mode_sends_labelled_page_images_then_the_text(tmp_path):
    fake = Fake()
    c = _client(fake, pdf_input="images", image_dpi=72, image_pages="figures")
    assert c.supports_pdf
    c.complete("sys", "Text:\n<<<paper>>>", pdf_path=_pdf(tmp_path))
    parts = fake.messages[0][1]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url", "text", "image_url", "text"]
    assert parts[0]["text"] == "[PDF page 2]" and parts[2]["text"] == "[PDF page 3]"
    assert base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1])[:4] == b"\x89PNG"
    assert parts[-1] == {"type": "text", "text": "Text:\n<<<paper>>>"}          # the text stays last
    assert not any(p["type"] == "file" for p in parts)                         # the PDF itself is not sent
    assert c.last_pdf_ref == "images:paper.pdf pages=2,3 dpi=72"


def test_cache_control_marks_system_and_last_user_block_and_keeps_the_prefix(tmp_path):
    fake = Fake([('{"source": nu', "length"), ('ll, "conditions": [], "observations": []}', "stop")])
    c = _client(fake, pdf_input="images", image_dpi=72, cache_control=True)
    c.complete("SYSTEM PROMPT", "Text:\n<<<paper>>>", pdf_path=_pdf(tmp_path))
    first, second = fake.messages
    assert first[0] == {"role": "system", "content": [
        {"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}]}
    user = first[1]["content"]
    assert user[-1]["cache_control"] == {"type": "ephemeral"}
    assert sum("cache_control" in p for p in user) == 1                        # one marker, on the last block
    assert second[:2] == first[:2]                                             # continuation: same cached prefix
    assert [m["role"] for m in second[2:]] == ["assistant", "user"]


def test_cache_control_also_works_without_a_pdf():
    fake = Fake()
    _client(fake, cache_control=True).complete("sys", "plain text")
    msgs = fake.messages[0]
    assert msgs[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[1]["content"] == [{"type": "text", "text": "plain text", "cache_control": {"type": "ephemeral"}}]


def test_cache_control_off_leaves_requests_unchanged():
    fake = Fake()
    _client(fake).complete("sys", "plain text")
    assert fake.messages[0][:2] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "plain text"}]


def test_settings(monkeypatch):
    assert _pdf_mode("Images") == "images"
    monkeypatch.setenv("LLM_PDF_INPUT", "images")
    monkeypatch.setenv("LLM_IMAGE_DPI", "110")
    monkeypatch.setenv("LLM_IMAGE_PAGES", "all")
    monkeypatch.setenv("LLM_CACHE_CONTROL", "1")
    s = load_settings()
    assert (s.llm_pdf_input, s.llm_image_dpi, s.llm_image_pages, s.llm_cache_control) == ("images", 110, "all", True)
    c = OpenAICompatClient.from_settings(s)
    assert (c.image_dpi, c.image_pages, c.cache_control) == (110, "all", True)
    monkeypatch.setenv("LLM_IMAGE_PAGES", "some")
    with pytest.raises(ValueError):
        load_settings()


def test_prompt_mentions_page_images_only_in_images_mode():
    assert "PAGE IMAGES" in extract.build_system_prompt(with_pdf=True, pdf_mode="images")
    assert "PAGE IMAGES" not in extract.build_system_prompt(with_pdf=True)
