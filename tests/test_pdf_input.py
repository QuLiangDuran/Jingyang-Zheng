"""Sending the original PDF to the model (LLM_PDF_INPUT = file_id | inline | off)."""
import base64
from types import SimpleNamespace

import pytest

from audit import extract
from audit.checks import check_record
from audit.config import _pdf_mode
from audit.extract import add_document, extract_document
from audit.fields import FieldValue, RecordContent
from audit.llm import OpenAICompatClient, ReplayClient


class FakeOpenAI:
    """Records what the client sends; no network."""

    def __init__(self):
        self.uploads, self.deleted, self.messages = [], [], []
        self.files = SimpleNamespace(create=self._create, delete=self.deleted.append)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))

    def _create(self, file, purpose):
        self.uploads.append((file[0], purpose))
        return SimpleNamespace(id=f"file-{len(self.uploads)}")

    def _chat(self, model, temperature, messages, **kw):
        self.messages.append(messages)
        msg = SimpleNamespace(content='{"source": null, "conditions": [], "observations": []}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def _client(mode):
    fake = FakeOpenAI()
    return OpenAICompatClient("http://x", "m", "k", pdf_input=mode, client=fake), fake


def test_file_id_mode_uploads_once_and_refers_by_id(pdf):
    c, fake = _client("file_id")
    c.complete("sys", "part 1", pdf_path=pdf)
    c.complete("sys", "part 2", pdf_path=pdf)                    # chunks of one paper reuse the upload
    assert fake.uploads == [("paper.pdf", "user_data")]
    parts = fake.messages[-1][1]["content"]
    assert parts[0] == {"type": "file", "file": {"file_id": "file-1"}}
    assert parts[1] == {"type": "text", "text": "part 2"}
    assert c.last_pdf_ref == "file_id:file-1"
    c.cleanup()
    assert fake.deleted == ["file-1"]


def test_inline_mode_sends_base64(pdf):
    c, fake = _client("inline")
    c.complete("sys", "text", pdf_path=pdf)
    part = fake.messages[0][1]["content"][0]["file"]
    assert part["filename"] == "paper.pdf" and fake.uploads == []
    assert base64.b64decode(part["file_data"].split(",", 1)[1]) == b"%PDF-1.4 fake"


def test_off_mode_sends_plain_text(pdf):
    c, fake = _client("off")
    assert not c.supports_pdf
    c.complete("sys", "text", pdf_path=pdf)
    assert fake.messages[0][1]["content"] == "text"


def test_bad_mode_is_refused():
    assert _pdf_mode(" Inline ") == "inline"
    with pytest.raises(ValueError):
        _pdf_mode("files")


def test_extraction_attaches_pdf_and_adds_figure_rules(conn, pdf):
    doc = add_document(conn, "p", "Some paper text.", 200_000, str(pdf))
    llm = ReplayClient([])
    llm.supports_pdf = True
    extract_document(conn, doc, llm)
    assert llm.pdf_paths == [str(pdf)]
    assert "ORIGINAL PDF ATTACHED" in llm.calls[0][0]
    run = conn.execute("SELECT prompt FROM extraction_runs WHERE document_id=?", (doc,)).fetchone()[0]
    assert run.startswith(f"[attached pdf]\n{pdf}")


def test_no_pdf_rules_without_pdf(conn, tmp_path):
    doc = add_document(conn, "p", "Some paper text.", 200_000, str(tmp_path / "paper.txt"))
    llm = ReplayClient([])
    llm.supports_pdf = True
    extract_document(conn, doc, llm)
    assert llm.pdf_paths == [None] and "ORIGINAL PDF ATTACHED" not in llm.calls[0][0]
    assert "ORIGINAL PDF ATTACHED" not in extract.build_system_prompt()


def _obs(**kw):
    return RecordContent(fields={k: FieldValue(value=v[0], quote=v[1]) for k, v in kw.items()})


TEXT = "Figure 3. Photocurrent output of the BPV systems under light-dark cycles."
CAPTION = "Photocurrent output of the BPV systems"


def test_graph_value_quotes_the_caption_and_gets_a_review_warning():
    obs = _obs(extraction_method=("graph_estimated", None), source_locator=("Figure 3a", None),
               value_original=("0.9", CAPTION), value_numeric=("0.9", CAPTION))
    codes = {(f["code"], f["field"]) for f in check_record("observation", obs, TEXT)}
    assert ("graph_value", "value_original") in codes
    assert not {c for c, fld in codes if c in ("value_not_in_quote", "value_numeric_mismatch")}
    assert ("quote_not_found", "value_original") not in codes      # caption is in the text


def test_graph_value_needs_a_figure_locator():
    obs = _obs(extraction_method=("graph_estimated", None), source_locator=("page 6", None),
               value_original=("0.9", CAPTION))
    assert "graph_locator_missing" in {f["code"] for f in check_record("observation", obs, TEXT)}


def test_text_value_still_must_be_in_its_quote():
    obs = _obs(extraction_method=("text", None), source_locator=("Results", None),
               value_original=("0.9", CAPTION), value_numeric=("0.9", CAPTION))
    assert "value_numeric_mismatch" in {f["code"] for f in check_record("observation", obs, TEXT)}
