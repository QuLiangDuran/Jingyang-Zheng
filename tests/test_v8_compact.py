"""v8: compact answers (quote ids, condition templates) and request/usage logging."""
import json
from types import SimpleNamespace

from audit.extract import add_document, extract_document, parse_response
from audit.llm import OpenAICompatClient, usage_summary


COMPACT = {
    "quotes": {"q1": "Chlamydomonas reinhardtii CC-125", "q2": "TAP medium", "q3": "Voc of 775 mV"},
    "source": {"doi": ["10.1/x", None], "paper_type": "original_research"},
    "condition_templates": {"T1": {"organism_name_reported": ["Chlamydomonas reinhardtii", "q1"],
                                   "medium_name": ["TAP medium", "q2"], "not_stated": ["feed_mode"]}},
    "conditions": [{"condition_key": "C1", "template": "T1", "group_type": ["µPSC-1", "q3"]},
                   {"condition_key": "C2", "template": "T1", "medium_name": None, "not_stated": ["mediator"]}],
    "observations": [{"condition_ref": "C1", "metric_code": "open_circuit_voltage",
                      "value_original": ["775", "q3"], "unit_original": ["mV", "q3"], "value_numeric": "775"}],
}


def test_compact_answer_expands_quotes_and_templates():
    p = parse_response(json.dumps(COMPACT))
    c1, c2 = p.conditions
    assert c1.fields["organism_name_reported"].quote == "Chlamydomonas reinhardtii CC-125"   # from the template
    assert c1.fields["group_type"].value == "µPSC-1" and c1.fields["group_type"].quote == "Voc of 775 mV"
    assert c1.not_stated == ["feed_mode"]
    assert "medium_name" not in c2.fields or not c2.fields["medium_name"].value                # null removes it
    assert c2.not_stated == ["feed_mode", "mediator"]
    o = p.observations[0]
    assert o.fields["value_original"].quote == "Voc of 775 mV" and o.fields["value_numeric"].quote is None


def test_unknown_quote_id_and_template_are_reported():
    bad = dict(COMPACT, conditions=[{"condition_key": "C9", "template": "T7", "organism_name_reported": ["X", "q9"]}])
    p = parse_response(json.dumps(bad))
    assert any("unknown template T7" in d for d in p.dropped_fields)
    assert any("unknown quote id q9" in d for d in p.dropped_fields)
    assert p.conditions[0].fields["organism_name_reported"].quote is None   # flagged quote_missing later


def test_long_form_answers_still_parse():
    old = {"source": None, "conditions": [{"condition_key": "C1", "organism_name_reported": {"value": "X", "quote": "X"}}],
           "observations": []}
    assert parse_response(json.dumps(old)).conditions[0].fields["organism_name_reported"].quote == "X"


def test_usage_summary_reads_openai_and_deepseek_style_counts():
    openai_u = SimpleNamespace(prompt_tokens=100, completion_tokens=50,
                               prompt_tokens_details=SimpleNamespace(cached_tokens=80),
                               completion_tokens_details=SimpleNamespace(reasoning_tokens=30))
    s = usage_summary(openai_u)
    assert (s["cached_tokens"], s["reasoning_tokens"]) == (80, 30) and s["raw"]["prompt_tokens"] == 100
    ds = usage_summary({"prompt_tokens": 100, "completion_tokens": 50, "prompt_cache_hit_tokens": 64,
                        "completion_tokens_details": {"reasoning_tokens": 20}})
    assert (ds["cached_tokens"], ds["reasoning_tokens"]) == (64, 20)


class Fake:
    def __init__(self):
        self.kw = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))

    def _chat(self, model, temperature, messages, **kw):
        self.kw = kw
        u = SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=SimpleNamespace(cached_tokens=8),
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=2))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(COMPACT)),
                                                        finish_reason="stop")], usage=u)


def test_request_options_and_usage_are_stored(conn):
    fake = Fake()
    c = OpenAICompatClient("http://x", "m", "k", max_output_tokens=1000, client=fake, reasoning_effort="low",
                           extra_body={"thinking": {"type": "disabled"}}, json_mode=True)
    doc = add_document(conn, "p", "Chlamydomonas reinhardtii CC-125 in TAP medium; Voc of 775 mV.", 200_000, None)
    extract_document(conn, doc, c)
    assert fake.kw == {"max_tokens": 1000, "reasoning_effort": "low", "response_format": {"type": "json_object"},
                       "extra_body": {"thinking": {"type": "disabled"}}}
    u = json.loads(conn.execute("SELECT usage FROM extraction_runs").fetchone()[0])
    assert (u["cached_tokens"], u["reasoning_tokens"], u["continuations"]) == (8, 2, 0)
    assert u["request_options"]["reasoning_effort"] == "low"
    assert conn.execute("SELECT COUNT(*) FROM records WHERE kind='condition'").fetchone()[0] == 2


def test_usage_report_separates_api_output_from_saved_output(conn):
    from audit.usage_report import format_report, report
    c = OpenAICompatClient("http://x", "m", "k", client=Fake())
    doc = add_document(conn, "p", "Chlamydomonas reinhardtii CC-125 in TAP medium; Voc of 775 mV.", 200_000, None)
    extract_document(conn, doc, c)
    (r,) = report(conn, doc)
    assert r["api_output_tokens"] == 5 and r["cached_tokens"] == 8 and r["reasoning_tokens"] == 2
    assert r["saved_unique_chars"] <= r["saved_chars"]
    text = format_report([r])
    assert "cache hits 8" in text and "API output (cumulative) 5" in text and "effective output" in text
