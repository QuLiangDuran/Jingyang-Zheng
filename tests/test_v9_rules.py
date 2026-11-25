"""v9 changes found on 2024_VICENTE-GARCIA (qwen3.8-flash, v8). Numbers match docs/PLAN.md #36."""
import json

from audit import extract
from audit.checks import check_record, record_key
from audit.extract import add_document, extract_document, parse_response
from audit.fields import FieldValue, RecordContent
from audit.llm import ReplayClient

ANSWER = {
    "quotes": {"q1": "Phaeodactylum tricornutum CCAP 1055/1 was grown in f/2 medium",
               "q2": "ITO anode poised at 0.23 V with 0.5 mM DCBQ",
               "q3": "Light intensity series"},
    "source": {"doi": ["10.1/x", None], "paper_type": "original_research"},
    "condition_templates": {
        "T_growth": {"quote": "q1", "organism_name_reported": "Phaeodactylum tricornutum",
                     "medium_name": "f/2 medium", "condition_source_locator": "Methods 2.1"},
        "T_anode": {"quote": "q2", "anode_material": "ITO", "applied_potential_value": 0.23,
                    "mediator_concentration": ["0.5", "q2"], "condition_source_locator": "Methods 2.3"},
    },
    "conditions": [
        {"condition_key": "Pht_anode_10", "template": ["T_growth", "T_anode"], "quote": "q3",
         "assay_light_intensity": 10},
        {"condition_key": "Pht_anode_only", "template": "T_anode"},
    ],
    "observations": [{"condition_ref": "Pht_anode_10", "metric_code": "photocurrent_density",
                      "value_original": ["~0.04", "q3"]}],
}


def _cond(p, key):
    return next(c for c in p.conditions if c.get("condition_key") == key)


# ---------- #1 several templates per condition ----------

def test_v9_1_condition_merges_several_templates_in_order():
    c = _cond(parse_response(json.dumps(ANSWER)), "Pht_anode_10")
    assert c.get("organism_name_reported") == "Phaeodactylum tricornutum"      # from T_growth
    assert c.get("anode_material") == "ITO"                                      # from T_anode
    assert c.get("condition_source_locator") == "Methods 2.3"                    # later template wins
    assert c.get("assay_light_intensity") == "10"                               # the condition's own setting


def test_v9_1_condition_without_organism_is_reported_as_such():
    c = _cond(parse_response(json.dumps(ANSWER)), "Pht_anode_only")
    codes = {(f["code"], f["field"]) for f in check_record("condition", c, "")}
    assert ("no_organism", "organism_name_reported") in codes
    assert ("missing_required", "organism_name_reported") not in codes


# ---------- #2 default quote (v10: observations only; see test_v10_rules.py) ----------

def test_v9_2_explicit_quote_ids_in_templates_still_resolve():
    c = _cond(parse_response(json.dumps(ANSWER)), "Pht_anode_10")
    assert c.fields["mediator_concentration"].quote == ANSWER["quotes"]["q2"]
    assert c.fields["condition_source_locator"].quote is None                    # no-quote field stays bare


# ---------- #7 duplicate conditions ----------

def test_v9_7_conditions_that_differ_only_in_key_are_duplicates():
    a = RecordContent(fields={"condition_key": FieldValue(value="Tw_bioanode"), "genus": FieldValue(value="T")})
    b = RecordContent(fields={"condition_key": FieldValue(value="Tw_bioanode_500uM"), "genus": FieldValue(value="T")})
    assert record_key(a, ignore=("condition_key",)) == record_key(b, ignore=("condition_key",))
    assert record_key(a) != record_key(b)


def test_v9_7_duplicate_condition_is_flagged(conn):
    ans = dict(ANSWER, conditions=[
        {"condition_key": "A", "template": ["T_growth", "T_anode"]},
        {"condition_key": "B", "template": ["T_growth", "T_anode"]}],
        observations=[{"condition_ref": "A", "metric_code": "photocurrent_density", "value_original": ["1", "q3"]},
                      {"condition_ref": "B", "metric_code": "photocurrent_density", "value_original": ["2", "q3"]}])
    doc = add_document(conn, "t", "text", 200_000)
    extract_document(conn, doc, ReplayClient([json.dumps(ans)]))
    flags = [json.loads(r[0]) for r in conn.execute("SELECT flags_json FROM records WHERE kind='condition'")]
    assert all(any(f["code"] == "duplicate" for f in fl) for fl in flags)


# ---------- #3 a series without its x value ----------

def test_v9_3_graph_values_of_one_condition_without_x_are_flagged(conn):
    obs = [{"condition_ref": "Pht_anode_10", "metric_code": "photocurrent_density", "extraction_method": "graph_estimated",
            "source_locator": "Figure 3b", "value_original": [v, "q3"], "value_numeric": v} for v in ("0.04", "0.07", "0.18")]
    doc = add_document(conn, "t", "text", 200_000)
    extract_document(conn, doc, ReplayClient([json.dumps(dict(ANSWER, observations=obs))]))
    rows = [json.loads(r[0]) for r in conn.execute("SELECT flags_json FROM records WHERE kind='observation'")]
    assert all(any(f["code"] == "series_without_x" for f in fl) for fl in rows)


def test_v9_3_one_graph_value_per_condition_is_fine(conn):
    obs = [{"condition_ref": "Pht_anode_10", "metric_code": "photocurrent_density", "extraction_method": "graph_estimated",
            "source_locator": "Figure 3b", "value_original": ["0.04", "q3"], "value_numeric": "0.04"}]
    doc = add_document(conn, "t", "text", 200_000)
    extract_document(conn, doc, ReplayClient([json.dumps(dict(ANSWER, observations=obs))]))
    fl = json.loads(conn.execute("SELECT flags_json FROM records WHERE kind='observation'").fetchone()[0])
    assert not any(f["code"] == "series_without_x" for f in fl)


# ---------- prompt v9 ----------

def test_prompt_v9_rules():
    assert extract.PROMPT_VERSION in ("v9", "v10")
    prompt = extract.build_system_prompt()
    for text in (
        '"template": ["T1", "T2"]',                        # 1
        'a default "quote": "qN"',                          # 2 (v10: observations only)
        "condition_source_locator in the template",         # 2
        "EACH point of a series",                           # 3
        "do not read that figure",                          # 3
        "the sentence specific to it",                      # 4
        "the same result",                                  # 5
        "a number written in value_original",              # 6
        "Do not create two identical conditions",           # 7
    ):
        assert text in prompt, text
    assert "one for cultivation, one per device); do not repeat" not in prompt   # the old contradiction
    ex = extract.response_example()
    assert ex["conditions"][0]["template"] == ["T1", "T2"]
