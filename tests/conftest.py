import json
from pathlib import Path

import pytest

from audit.cli import AnchoredReplayClient
from audit.db import connect
from audit.documents import load_text
from audit.extract import add_document, extract_document

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def load_demo(conn, max_chars: int = 200_000, llm=None) -> int:
    """Default: the whole demo paper in one call. A small max_chars forces the chunked path."""
    text = load_text(SAMPLES / "sample_paper.txt")
    entries = json.loads((SAMPLES / "sample_responses.json").read_text(encoding="utf-8"))
    doc_id = add_document(conn, "demo", text, max_chars)
    extract_document(conn, doc_id, llm or AnchoredReplayClient(entries))
    return doc_id


def demo_client() -> AnchoredReplayClient:
    return AnchoredReplayClient(json.loads((SAMPLES / "sample_responses.json").read_text(encoding="utf-8")))


@pytest.fixture
def demo(conn):
    return load_demo(conn)
