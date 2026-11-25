"""v11 changes (VIC v10 + KUR v10 review). Numbers match docs/PLAN.md #38."""
import json

from audit import extract, review_service
from audit.checks import check_record
from audit.extract import add_document, extract_document, parse_response
from audit.fields import FIELDS_BY_NAME, TEMP_METRIC_CODES, FieldValue, RecordContent
from audit.llm import ReplayClient

Q = {"q1": "C. reinhardtii CC-125 was grown in TAP medium", "q2": "Figure 3b shows the photocurrent"}


def _answer(conditions, observations, templates=None):
    return {"quotes": Q, "source": {"doi": ["10.1/x", None]},
            "condition_templates": templates if templates is not None else {
                "T": {"organism_name_reported": ["C. reinhardtii", "q1"], "medium_name": "TAP medium",
                      "condition_source_locator": "Methods"}},
            "conditions": conditions, "observations": observations}


def _run(conn, ans, text="text"):
    doc = add_document(conn, "t", text, 200_000)
    extract_document(conn, doc, ReplayClient([json.dumps(ans)]))
    return doc


def _flags(conn, kind):
    return [json.loads(r[0]) for r in conn.execute("SELECT flags_json FROM records WHERE kind=? ORDER BY id", (kind,))]


def _obs(ref, value="1", **kw):
    o = {"condition_ref": ref, "metric_code": "open_circuit_voltage", "extraction_method": "text",
         "value_status": "reported", "value_original": [value, "q1"], "unit_original": ["mV", "q1"]}
    o.update(kw)
    return o


# ---------- #5 a template field missing its quote counts once ----------

def test_v11_5_template_fields_remember_where_they_came_from():
    p = parse_response(json.dumps(_answer([{"condition_key": "A", "template": "T", "medium_name": ["TAP", "q1"]}], [])))
    c = p.conditions[0]
    assert c.from_template == {"organism_name_reported": "T", "condition_source_locator": "T"}   # own medium_name
    assert RecordContent.model_validate_json(c.model_dump_json()).from_template == c.from_template


def test_v11_5_quote_missing_of_a_template_field_is_reported_once(conn):
    conds = [{"condition_key": k, "template": "T", "group_type": k} for k in ("A", "B", "C")]
    _run(conn, _answer(conds, [_obs(k) for k in ("A", "B", "C")]))
    hits = [f for fl in _flags(conn, "condition") for f in fl
            if f["code"] == "quote_missing" and f["field"] == "medium_name"]
    assert len(hits) == 1 and "另有 2 个条件" in hits[0]["message"]


def test_v11_5_an_edited_field_is_no_longer_from_the_template(conn):
    conds = [{"condition_key": k, "template": "T", "group_type": k} for k in ("A", "B")]
    doc = _run(conn, _answer(conds, [_obs("A"), _obs("B")]))
    first = conn.execute("SELECT id FROM records WHERE kind='condition' ORDER BY id").fetchone()[0]
    review_service.edit_field(conn, first, "tester", "medium_name", value="TAP medium",
                              quote="C. reinhardtii CC-125 was grown in TAP medium", reason="quote")
    c = RecordContent.model_validate_json(conn.execute("SELECT current_json FROM records WHERE id=?", (first,)).fetchone()[0])
    assert "medium_name" not in c.from_template
    second = _flags(conn, "condition")[1]
    assert any(f["code"] == "quote_missing" and f["field"] == "medium_name" for f in second)   # now reported on B


# ---------- #6 conditions without observations; observations first ----------

def test_v11_6_condition_without_observation_is_flagged(conn):
    conds = [{"condition_key": "A", "template": "T"}, {"condition_key": "B", "template": "T", "group_type": "b"}]
    _run(conn, _answer(conds, [_obs("A")]))
    a, b = _flags(conn, "condition")
    assert not any(f["code"] == "condition_without_observation" for f in a)
    assert any(f["code"] == "condition_without_observation" for f in b)


# ---------- #3 metric_code: no "other"; Yuntian names in the temporary list ----------

def test_v11_3_metric_list_has_the_names_of_the_manual_sheet():
    for m in ("short_circuit_current", "load_voltage", "load_current", "chloroplast_count",
              "detached_cell_fraction", "photocurrent_retention", "internal_quantum_efficiency", "power", "max_power"):
        assert m in TEMP_METRIC_CODES, m


def test_v11_3_metric_other_is_an_error():
    c = RecordContent(fields={"metric_code": FieldValue(value="other")})
    assert ("metric_other", "error") in {(f["code"], f["level"]) for f in check_record("observation", c, "")}


# ---------- #4 the unit must fit the metric ----------

def _unit_flags(metric, unit):
    c = RecordContent(fields={"metric_code": FieldValue(value=metric), "unit_original": FieldValue(value=unit)})
    return {f["code"] for f in check_record("observation", c, "")}


def test_v11_4_power_units_are_power_and_density_needs_an_area():
    assert "unit_metric_mismatch" in _unit_flags("max_power_density", "µW")      # KUR Pmp 926.4 µW
    assert "unit_metric_mismatch" in _unit_flags("current_density", "µA")
    assert "unit_metric_mismatch" in _unit_flags("max_power", "mW/m2")
    for metric, unit in (("max_power", "µW"), ("max_power_density", "mW/m2"), ("power_density", "mWm−2"),
                         ("photocurrent_density", "µA/cm2"), ("current", "µA")):
        assert "unit_metric_mismatch" not in _unit_flags(metric, unit), (metric, unit)


# ---------- #2 a series that looks made up ----------

def _series(conn, xs, ys):
    conds = [{"condition_key": f"L{x}", "template": "T", "assay_light_intensity": str(x)} for x in xs]
    obs = [_obs(f"L{x}", str(y), metric_code="photocurrent_density", unit_original=["µA/cm2", "q2"],
                value_original=[f"~{y}", "q2"], value_numeric=str(y), extraction_method="graph_estimated",
                value_status="approximate", source_locator="Figure 3b") for x, y in zip(xs, ys)]
    _run(conn, _answer(conds, obs))
    return [any(f["code"] == "suspected_fabricated_series" for f in fl) for fl in _flags(conn, "observation")]


def test_v11_2_even_x_and_repeated_y_is_suspected(conn):
    assert all(_series(conn, [1, 2, 3, 4, 5, 6], [0.1, 0.16, 0.2, 0.2, 0.2, 0.25]))


def test_v11_2_real_axis_values_are_fine(conn):
    assert not any(_series(conn, [10, 30, 54, 100, 140], [0.04, 0.07, 0.18, 0.19, 0.19]))


def test_v11_2_even_x_alone_is_fine(conn):
    assert not any(_series(conn, [50, 100, 150, 200, 250], [0.05, 0.08, 0.1, 0.12, 0.09]))


# ---------- prompt v11 ----------

def test_prompt_v11():
    assert extract.PROMPT_VERSION == "v11"
    prompt = extract.build_system_prompt(with_pdf=True)
    for text in (
        "as many observations as points you can see",          # 1
        "never add a point",                                    # 1
        "read the x value against the axis",                    # 1
        "never assume even spacing",                            # 1
        "metric_code is never other",                           # 3
        "a unit of power (W, mW, µW) is a power",              # 4
        "Write the observations first",                         # 6
        "Every observation states extraction_method and value_status",  # 7
        "reference group",                                      # 8
        "in the Text and in a table",                           # 8
    ):
        assert text in prompt, text
    assert '- "metric_code" (' in prompt and "max_power | other" not in prompt
    ex = extract.response_example()
    assert list(ex) == ["quotes", "source", "observations", "condition_templates", "conditions"]
    assert {"extraction_method", "value_status"} <= set(ex["observations"][0])
