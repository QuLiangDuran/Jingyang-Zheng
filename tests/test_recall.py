"""v10 #3: recall check -- result sentences that no observation quotes (no API call)."""
import json

from audit.extract import add_document, extract_document
from audit.llm import ReplayClient
from audit.recall import coverage, result_region, result_sentences

TEXT = """Title. Introduction. Earlier work reached 100 mW/m2 [3].

■EXPERIMENTAL SECTION

Cells were grown at 20 °C in f/2 medium.

■RESULTS AND DISCUSSION

The bioanode gave ∼100 nA/cm2 at the fourth cycle. Fewer than 1% of Pht cells detach from the bioanode. Each Fp cell has ∼8−12 chloroplasts. This shows the method works. FDA activity was ∼23% vs ∼6% after drying.

■CONCLUSION In summary, diatoms reached 0.3 μA cm−2.

■REFERENCES

(1) Smith 2020, 12, 345.
"""


def _sents(text):
    return [text[a:b].strip() for a, b in result_sentences(text)]


def test_region_is_results_and_discussion_only():
    a, b = result_region(TEXT)
    assert TEXT[a:b].lstrip("■ ").startswith("RESULTS AND DISCUSSION")
    assert "CONCLUSION" not in TEXT[a:b] and "20 °C" not in TEXT[a:b]


def test_result_sentences_need_a_number_with_unit_percent_or_approximation():
    s = _sents(TEXT)
    assert s == ["The bioanode gave ∼100 nA/cm2 at the fourth cycle.",
                 "Fewer than 1% of Pht cells detach from the bioanode.",
                 "Each Fp cell has ∼8−12 chloroplasts.",
                 "FDA activity was ∼23% vs ∼6% after drying."]


def test_region_falls_back_to_text_before_references():
    t = "Intro.\n\nIt gave 5 mV here.\n\nReferences\n\n(1) X, 2020, 12 %."
    assert _sents(t) == ["It gave 5 mV here."]


def test_mdpi_and_nature_headings():
    mdpi = "2. Materials and Methods\n\nGrown at 30 °C.\n\n3. Results\n\nIt gave 0.4 µA/cm2.\n\n5. Conclusions\n\nIt gave 9 V."
    assert _sents(mdpi) == ["It gave 0.4 µA/cm2."]
    nature = "Introduction\n\nOld 3 V.\n\nResults and discussions Open circuit voltage was 775 mV.\n\nConclusion\n\nAll 9 V."
    assert _sents(nature) == ["Results and discussions Open circuit voltage was 775 mV."]


def test_coverage_counts_sentences_quoted_by_an_observation():
    quotes = ["Fewer than 1% of Pht cells detach", "gave ∼100 nA/cm2"]
    total, uncovered = coverage(TEXT, quotes)
    assert total == 4
    assert [TEXT[a:b].strip() for a, b in uncovered] == ["Each Fp cell has ∼8−12 chloroplasts.",
                                                          "FDA activity was ∼23% vs ∼6% after drying."]


def test_uncovered_results_are_flagged_on_the_paper(conn):
    ans = {"source": {"doi": {"value": "10.1/x", "quote": None}},
           "conditions": [{"condition_key": "c1", "organism_name_reported": {"value": "Pht", "quote": "Pht"}}],
           "observations": [{"condition_ref": "c1", "metric_code": {"value": "photocurrent_density"},
                             "value_original": {"value": "∼100 nA/cm2", "quote": "The bioanode gave ∼100 nA/cm2"}}]}
    doc = add_document(conn, "t", TEXT, 200_000)
    extract_document(conn, doc, ReplayClient([json.dumps(ans)]))
    flags = json.loads(conn.execute("SELECT flags_json FROM records WHERE kind='source'").fetchone()[0])
    msgs = [f["message"] for f in flags if f["code"] == "uncovered_result"]
    assert len(msgs) == 3 and any("Fewer than 1%" in m for m in msgs)
    assert not any("∼100 nA/cm2" in m for m in msgs)
