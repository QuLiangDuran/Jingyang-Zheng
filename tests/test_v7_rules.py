"""Prompt v7 / checker changes found on 2025_YUAN (document 3). Issue numbers match docs/PLAN.md #31."""
import json

from audit import extract
from audit.checks import check_record
from audit.extract import parse_response
from audit.fields import FIELDS_BY_NAME, FieldValue, RecordContent


def _content(level, not_stated=(), **values):
    c = RecordContent.empty(level)
    for k, v in values.items():
        c.fields[k] = FieldValue(value=v[0], quote=v[1]) if isinstance(v, tuple) else FieldValue(value=v)
    c.not_stated = list(not_stated)
    return c


def _codes(kind, c, text="", electrical=False):
    return {(f["code"], f["field"], f["level"]) for f in check_record(kind, c, text, electrical)}


# ---------- R1: classification labels take no quote; units still do ----------

def test_r1_classification_labels_need_no_quote_but_units_do():
    for name in ("organism_group", "genotype_status", "condition_phase", "light_measurement_basis",
                 "biomass_loading_basis", "inoculum_basis", "normalization_basis"):
        assert not FIELDS_BY_NAME[name].needs_quote, name
    for name in ("light_unit", "biomass_loading_unit", "unit_original"):
        assert FIELDS_BY_NAME[name].needs_quote, name
    c = _content("condition", organism_group="cyanobacterium", light_unit="µmol photons/m2/s")
    codes = {(code, f) for code, f, _ in _codes("condition", c)}
    assert ("quote_missing", "organism_group") not in codes
    assert ("quote_missing", "light_unit") in codes


def test_r1_value_numeric_uses_the_quote_of_value_original():
    text = "the photocurrent was 0.414 µA/cm2 in HL7942"
    ok = _content("observation", value_original=("0.414 µA/cm2", "0.414 µA/cm2"), value_numeric="0.414")
    assert not {x for x in _codes("observation", ok, text) if x[1] == "value_numeric"}
    alone = _content("observation", value_numeric="0.414")
    assert ("quote_missing", "value_numeric", "error") in _codes("observation", alone, text)


# ---------- R2: not_stated separates "the paper does not say" from "the AI missed it" ----------

def test_r2_not_stated_is_parsed_kept_and_filtered():
    raw = json.dumps({"source": None, "observations": [], "conditions": [
        {"condition_key": "c1", "genus": {"value": "Synechococcus", "quote": "Synechococcus"},
         "not_stated": ["feed_mode", "mediator", "bogus"]}]})
    p = parse_response(raw)
    assert p.conditions[0].not_stated == ["feed_mode", "mediator"]
    assert "conditions[0].not_stated.bogus" in p.dropped_fields
    again = RecordContent.model_validate_json(p.conditions[0].model_dump_json())
    assert again.not_stated == ["feed_mode", "mediator"]                    # survives the database round trip
    assert RecordContent.model_validate_json('{"fields": {}}').not_stated == []  # v6 records still load


def test_r2_listed_required_field_is_a_warning_unlisted_is_an_error():
    c = _content("condition", not_stated=["feed_mode"])
    codes = _codes("condition", c, electrical=True)
    assert ("not_stated", "feed_mode", "warning") in codes
    assert ("missing_required", "feed_mode", "error") not in codes
    assert ("missing_required", "mediator", "error") in codes               # not listed: the AI missed it


def test_r2_not_stated_is_ignored_when_the_field_has_a_value():
    c = _content("condition", not_stated=["feed_mode"], feed_mode=("batch", "batch"))
    assert not {x for x in _codes("condition", c, "batch") if x[1] == "feed_mode"}


# ---------- prompt v7 ----------

def test_prompt_v7_rules():
    assert extract.PROMPT_VERSION in ("v7", "v8", "v9", "v10", "v11")  # later versions keep every v7 rule
    prompt = extract.build_system_prompt()
    for text in (
        '"not_stated"',                                       # R2: list of required fields the paper does not give
        "mediator = none ONLY if the paper says",             # R2: no inference from the setup
        "Do not carry device settings from one mode",         # B1: C7-C10
        "relative difference",                                # B2: O20
        "ONE continuous span",                                # B3: O25 and the ITO quotes
    ):
        assert text in prompt, text
    ex = extract.response_example()
    assert ex["conditions"][0]["not_stated"] == ["..."]
    assert '- "value_numeric" (' in prompt and "quote of value_original" in prompt
