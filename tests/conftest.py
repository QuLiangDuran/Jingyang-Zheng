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


def load_demo(conn) -> int:
    text = load_text(SAMPLES / "sample_paper.txt")
    entries = json.loads((SAMPLES / "sample_responses.json").read_text(encoding="utf-8"))
    doc_id = add_document(conn, "demo", text, 600)
    extract_document(conn, doc_id, AnchoredReplayClient(entries))
    return doc_id


@pytest.fixture
def demo(conn):
    return load_demo(conn)
