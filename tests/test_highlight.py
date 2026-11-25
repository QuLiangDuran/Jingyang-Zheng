"""The source pane must show the original text exactly: highlights may not drop, repeat or cover characters."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from audit import review_service as svc

STATIC = Path(__file__).resolve().parent.parent / "audit" / "static"
NODE = shutil.which("node")

# Runs static/highlight.js (the code the page uses) on each case; returns the visible text and,
# per field, the concatenated text of the marks covering it.
_SCRIPT = r"""
const { highlightHtml } = require(process.argv[1]);
const unesc = (s) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = cases.map(({ text, ranges, seg }) => {
  const html = highlightHtml(text, ranges, seg);
  const marked = {};
  for (const m of html.matchAll(/<mark[^>]*data-f="([^"]*)"[^>]*>([\s\S]*?)<\/mark>/g))
    for (const i of m[1].split(",")) marked[i] = (marked[i] || "") + unesc(m[2]);
  return { visible: unesc(html.replace(/<[^>]*>/g, "")), marked };
});
process.stdout.write(JSON.stringify(out));
"""


def _run(cases: list[dict]) -> list[dict]:
    res = subprocess.run([NODE, "-e", _SCRIPT, str(STATIC / "highlight.js")], input=json.dumps(cases),
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(res.stdout)


def _case(text: str, rec: dict) -> dict:
    ranges = [{"s": f["highlight"][0], "e": f["highlight"][1], "i": i, "label": f["name"]}
              for i, f in enumerate(rec["fields"]) if f["highlight"]]
    return {"text": text, "ranges": ranges, "seg": {"start": rec["segment"]["start"], "end": rec["segment"]["end"]}}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_highlighted_text_equals_source_text(conn, demo):
    text = svc.get_document(conn, 1)["text"]
    recs = [svc.get_record(conn, r["id"]) for r in svc.list_records(conn)]
    tricky = {"text": 'a < b & "c" — µ 10 kΩ\nend', "seg": {"start": 2, "end": 30},   # entities, overlap, bad range
              "ranges": [{"s": 0, "e": 5, "i": 0, "label": "x"}, {"s": 3, "e": 12, "i": 1, "label": "y"},
                         {"s": 20, "e": 99, "i": 2, "label": "z"}]}
    results = _run([_case(text, r) for r in recs] + [tricky])
    for rec, out in zip(recs, results):
        assert out["visible"] == text, f"record {rec['id']}: visible text differs from source"
        for i, f in enumerate(rec["fields"]):
            if f["highlight"]:
                s, e = f["highlight"]
                assert out["marked"][str(i)] == text[s:e], f"record {rec['id']} field {f['name']}"
    assert results[-1]["visible"] == tricky["text"]
    # the cases reported from the browser
    c1 = results[[r["id"] for r in recs].index(2)]["marked"]
    names = [f["name"] for f in recs[1]["fields"]]
    assert c1[str(names.index("strain_id"))] == "Chlorella vulgaris strain DEMO-17"
    assert c1[str(names.index("working_volume"))] == "a working volume of 150 mL"


def test_highlight_css_cannot_cover_neighbouring_characters():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if re.search(r"(^|[\s,.])(mark|\.seg)\b", selector.strip()):
            assert not re.search(r"box-shadow|outline|padding|border|margin", body), f"{selector.strip()}: {body}"
