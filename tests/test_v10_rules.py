"""v10 changes (VIC v9 review). Numbers match docs/PLAN.md #37. The recall check (#3) is in test_recall.py."""
import json

from audit import extract
from audit.checks import check_record
from audit.extract import parse_response
from audit.fields import FIELDS_BY_NAME

QUOTES = {"q1": "Pht was grown in f/2 medium at 18 °C", "q2": "fewer than 1% of Pht cells detach"}


def test_v10_2_default_quote_applies_to_observations_only():
    ans = {"quotes": QUOTES, "source": None,
           "condition_templates": {"T": {"quote": "q1", "medium_name": "f/2 medium"}},
           "conditions": [{"condition_key": "c1", "template": "T", "quote": "q1", "temperature": "18"}],
           "observations": [{"condition_ref": "c1", "quote": "q2", "metric_code": "detachment_rate",
                             "value_original": "fewer than 1%", "unit_original": "%"}]}
    p = parse_response(json.dumps(ans))
    c, o = p.conditions[0], p.observations[0]
    assert c.fields["medium_name"].quote is None and c.fields["temperature"].quote is None   # no default here
    assert o.fields["value_original"].quote == QUOTES["q2"] and o.fields["unit_original"].quote == QUOTES["q2"]
    notes = " ".join(p.dropped_fields)
    assert "condition_templates.T.quote" in notes and "conditions[0].quote" in notes        # reported, not silent


def test_v10_4_group_type_is_a_label_without_quote_but_analyte_keeps_its_quote():
    assert not FIELDS_BY_NAME["group_type"].needs_quote
    assert FIELDS_BY_NAME["analyte"].needs_quote


def test_v10_prompt():
    assert extract.PROMPT_VERSION == "v10"
    prompt = extract.build_system_prompt()
    for text in (
        "every measured trait stated in the Text",              # 1
        "never replaces",                                        # 1
        "Only an observation may give a default",               # 2
        "every field of a template or condition takes its own quote id",  # 2
        'exactly as written, e.g. "P. tricornutum"',            # 2
    ):
        assert text in prompt, text
    ex = extract.response_example()
    assert "quote" not in ex["condition_templates"]["T1"] and ex["observations"][0]["quote"] == "q1"
