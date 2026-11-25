import json
import sqlite3

import pytest

from audit import review_service as svc
from audit.checks import check_record
from audit.fields import FIELDS_BY_NAME, RecordContent, choice_ok


def _field(rec, name):
    return next(f for f in rec["fields"] if f["name"] == name)


def _codes(rec, field=None):
    return {f["code"] for f in rec["flags"] if field is None or f["field"] == field}


def test_detail_has_highlights_inside_document(conn, demo):
    for rid in (1, 2, 4):
        r = svc.get_record(conn, rid)
        text = svc.get_document(conn, r["document_id"])["text"]
        for f in r["fields"]:
            if f["quote"]:
                s, e = f["highlight"]
                assert text[s:e] == f["quote"]


def test_detail_shows_ids_and_links(conn, demo):
    r = svc.get_record(conn, 4)
    assert r["kind"] == "observation" and r["system_id"] == "2025_EXAMPLE-O1"
    assert [p["id"] for p in r["parents"]] == [2, 1]            # condition, then paper
    c = svc.get_record(conn, 2)
    assert c["experiment_id"] == "2025_EXAMPLE-E1" and c["arm_id"] == "2025_EXAMPLE-A1"
    assert sorted(x["id"] for x in c["children"]) == [4, 6] and c["is_electrical"]


def test_edit_fixes_flag_and_logs_without_touching_proposed(conn, demo):
    before = conn.execute("SELECT proposed_json FROM records WHERE id=5").fetchone()[0]
    r = svc.edit_field(conn, 5, "张三", "value_numeric", "原文是 0.97", value="0.97")
    f = _field(r, "value_numeric")
    assert f["value"] == "0.97" and f["edited"] and f["proposed_value"] == "0.79"
    assert not r["flags"]                                       # also no longer a duplicate of #8
    assert "duplicate" not in _codes(svc.get_record(conn, 8))
    assert conn.execute("SELECT proposed_json FROM records WHERE id=5").fetchone()[0] == before
    [log] = r["log"]
    assert (log["action"], log["field"], log["old_value"], log["new_value"], log["reason"]) == \
        ("edit", "value_numeric", "0.79", "0.97", "原文是 0.97")


def test_editing_a_link_relinks_the_record(conn, demo):
    r = svc.edit_field(conn, 7, "a", "condition_ref", "原文是 35 °C 组", value="heat_35C")
    assert r["parents"][0]["id"] == 3 and not r["flags"]
    assert svc.get_record(conn, 3)["is_electrical"]             # condition now has an electrical result


def test_edit_validation(conn, demo):
    with pytest.raises(svc.Invalid):
        svc.edit_field(conn, 4, "", "analyte", "x", value="1")
    with pytest.raises(svc.Invalid):
        svc.edit_field(conn, 4, "a", "analyte", "", value="1")
    with pytest.raises(svc.Invalid):
        svc.edit_field(conn, 4, "a", "nope", "x", value="1")
    with pytest.raises(svc.Invalid):
        svc.edit_field(conn, 4, "a", "genus", "x", value="Chlorella")    # condition field on an observation
    with pytest.raises(svc.Invalid):
        svc.edit_field(conn, 4, "a", "review_status", "x", value="approved")  # reviewer field, not editable
    with pytest.raises(svc.Invalid):
        svc.edit_field(conn, 4, "a", "value_numeric", "x", value="1.42")      # no change
    with pytest.raises(svc.Invalid):
        svc.reject(conn, 4, "a", " ")
    with pytest.raises(svc.NotFound):
        svc.approve(conn, 999, "a")


def test_choice_outside_temporary_list_is_rejected_on_edit(conn, demo):
    with pytest.raises(svc.Invalid):
        svc.edit_field(conn, 3, "a", "habitat_class", "x", value="lake water")
    r = svc.edit_field(conn, 3, "a", "habitat_class", "原文写 freshwater", value="freshwater")
    assert "choice_not_allowed" not in _codes(r)
    r = svc.edit_field(conn, 3, "a", "habitat_class", "不确定", value="unspecified")  # always allowed
    assert "choice_not_allowed" not in _codes(r)


def test_choice_outside_temporary_list_from_ai_is_flagged_not_rewritten(conn, demo):
    r = svc.get_record(conn, 3)
    assert "choice_not_allowed" in _codes(r, "habitat_class")
    assert _field(r, "habitat_class")["value"] == "lake water"   # AI value kept as proposed
    spec = FIELDS_BY_NAME["light_unit"]
    assert choice_ok(spec, "µmol photons m−2 s−1") and choice_ok(spec, "OTHER") and not choice_ok(spec, "foot-candles")


def test_missing_required_fields_are_flagged():
    obs = RecordContent.empty("observation")
    codes = {(f["code"], f["field"]) for f in check_record("observation", obs, "")}
    for name in ("trait_group", "metric_code", "record_role", "analyte", "value_status", "source_locator",
                 "extraction_method", "condition_ref"):
        assert ("missing_required", name) in codes
    assert ("missing_required", "value_numeric") not in codes              # optional

    cond = RecordContent.empty("condition")
    plain = {f["field"] for f in check_record("condition", cond, "", electrical=False)}
    elec = {f["field"] for f in check_record("condition", cond, "", electrical=True)}
    assert "medium_name" in plain and "anode_material" not in plain        # electrical essentials only ...
    assert "anode_material" in elec                                        # ... for electrical conditions

    src = RecordContent.empty("source")
    src.fields["paper_verdict"].value = "exclude"
    fields = {f["field"] for f in check_record("source", src, "")}
    assert {"doi", "article_title", "paper_type", "exclusion_reason"} <= fields


def test_state_machine(conn, demo):
    svc.approve(conn, 4, "a")
    with pytest.raises(svc.Conflict):
        svc.approve(conn, 4, "a")
    with pytest.raises(svc.Conflict):
        svc.edit_field(conn, 4, "a", "value_numeric", "x", value="1")
    r = svc.reopen(conn, 4, "a", "看错了")
    assert r["status"] == "pending" and r["reviewed_at"] is None

    r = svc.flag_for_resolution(conn, 4, "a", "需要问作者")
    assert r["status"] == "needs_resolution"
    with pytest.raises(svc.Conflict):
        svc.flag_for_resolution(conn, 4, "a", "again")
    svc.edit_field(conn, 4, "a", "n_biological", "作者回复", value="3")   # still editable
    r = svc.approve(conn, 4, "a")
    assert [l["action"] for l in r["log"]] == ["approve", "reopen", "flag", "edit", "approve"]
    assert svc.reviewer_notes(conn, 4) == "[reopen] 看错了; [flag] 需要问作者; [edit n_biological] 作者回复"


def test_reject_clears_duplicate_flag_on_twin(conn, demo):
    assert "duplicate_observation" in _codes(svc.get_record(conn, 5))
    svc.reject(conn, 8, "a", "重复")
    assert "duplicate_observation" not in _codes(svc.get_record(conn, 5))


def test_duplicate_observation_ignores_quotes_but_not_unit_value_or_time(conn, demo):
    dup = lambda rid: "duplicate_observation" in _codes(svc.get_record(conn, rid))  # noqa: E731
    assert dup(5) and dup(8)
    assert "duplicate" not in _codes(svc.get_record(conn, 5))           # exact-duplicate flag is not used for observations
    svc.edit_field(conn, 8, "a", "analyte", "引自讨论", quote="dry biomass")   # different quote, same result
    assert dup(5) and dup(8)
    svc.edit_field(conn, 8, "a", "unit_original", "x", value="mg/L")
    assert not dup(5) and not dup(8)
    svc.edit_field(conn, 8, "a", "unit_original", "x", value="g/L")
    assert dup(8)
    svc.edit_field(conn, 8, "a", "time_value", "x", value="48")
    assert not dup(8)
    svc.edit_field(conn, 8, "a", "time_value", "x", value="")
    svc.edit_field(conn, 5, "a", "value_numeric", "原文 0.97", value="0.97")   # values now differ
    assert not dup(5) and not dup(8)


def test_exact_duplicate_still_applies_to_conditions(conn):
    from audit.extract import add_document, extract_document
    from audit.llm import ReplayClient
    doc = add_document(conn, "t", "Strain X in BG-11.", 1000)
    cond = {"condition_key": "c1", "genus": {"value": "Strain X", "quote": "Strain X"}}
    extract_document(conn, doc, ReplayClient([json.dumps({"conditions": [cond, cond]})]))
    ids = [r["id"] for r in svc.list_records(conn, kind="condition")]
    assert all("duplicate" in _codes(svc.get_record(conn, i)) for i in ids)


def test_missing_vs_sibling_is_a_warning_per_field(conn, demo):
    r = svc.get_record(conn, 3)
    sib = {f["field"]: f for f in r["flags"] if f["code"] == "missing_vs_sibling"}
    assert {"light_intensity", "photoperiod", "external_load_value"} <= set(sib)
    assert all(f["level"] == "warning" for f in sib.values()) and "2025_EXAMPLE-C1" in sib["photoperiod"]["message"]
    assert "medium_name" not in sib and "condition_key" not in sib    # filled / link fields are not compared
    r = svc.edit_field(conn, 3, "a", "photoperiod", "补上", value="16:8 h light:dark",
                       quote="with a 16:8 h light:dark cycle")
    assert "photoperiod" not in {f["field"] for f in r["flags"] if f["code"] == "missing_vs_sibling"}
    r = svc.edit_field(conn, 3, "a", "experiment_ref", "不是同一个实验", value="other_series")
    assert "missing_vs_sibling" not in _codes(r)                       # no sibling any more
    assert "missing_vs_sibling" not in _codes(svc.get_record(conn, 2))


@pytest.mark.parametrize("name", ["", "   ", None])
@pytest.mark.parametrize("action", ["approve", "reject", "flag", "reopen", "edit"])
def test_empty_reviewer_is_refused(conn, demo, action, name):
    if action == "reopen":
        svc.approve(conn, 4, "a")
    calls = {
        "approve": lambda: svc.approve(conn, 4, name),
        "reject": lambda: svc.reject(conn, 4, name, "原因"),
        "flag": lambda: svc.flag_for_resolution(conn, 4, name, "原因"),
        "reopen": lambda: svc.reopen(conn, 4, name, "原因"),
        "edit": lambda: svc.edit_field(conn, 4, name, "n_biological", "原因", value="3"),
    }
    before = conn.execute("SELECT status, current_json FROM records WHERE id=4").fetchone()
    log_before = conn.execute("SELECT COUNT(*) FROM review_log").fetchone()[0]
    with pytest.raises(svc.Invalid):
        calls[action]()
    assert tuple(conn.execute("SELECT status, current_json FROM records WHERE id=4").fetchone()) == tuple(before)
    assert conn.execute("SELECT COUNT(*) FROM review_log").fetchone()[0] == log_before


def _numeric_flags(value_numeric, value_original, quote=None):
    c = RecordContent.empty("observation")
    c.fields["value_numeric"].value = value_numeric
    c.fields["value_original"].value = value_original
    c.fields["value_original"].quote = quote if quote is not None else value_original
    return [(f["field"], f["level"]) for f in check_record("observation", c, quote or value_original or "")
            if f["code"] == "value_numeric_mismatch"]


@pytest.mark.parametrize("num, orig", [
    ("0.97", "0.97 ± 0.05"), ("0.970", "0.97±0.05"), ("1.5", "1.2–1.5"), ("1.2", "1.2-1.5"),
    ("1200000", "1.2 × 10^6"), ("1.2e6", "1.2 × 10⁶"), ("2000000", "1–2 × 10^6"), ("0.0021", "2.1 × 10⁻³"),
    ("-0.25", "-0.25"), ("3.4e-5", "3.4E-5"), ("1234.5", "1,234.5"),
])
def test_value_numeric_consistent(num, orig):
    assert _numeric_flags(num, orig) == []


@pytest.mark.parametrize("num, orig", [
    ("0.79", "0.97 ± 0.05"), ("-1.5", "1.2–1.5"), ("1.2", "1.2 × 10^6"), ("10", "1.2 × 10^6"), ("0.25", "-0.25"),
])
def test_value_numeric_mismatch_is_an_error(num, orig):
    assert _numeric_flags(num, orig) == [("value_numeric", "error")]


def test_value_original_must_be_in_its_quote():
    assert _numeric_flags("0.97", "0.97 ± 0.05", quote="dry biomass was 0.97 g/L") == [("value_original", "error")]
    assert _numeric_flags(None, "0.97 ± 0.05", quote="was 0.97 ± 0.05 g/L") == []
    assert _numeric_flags("0.97", None) == []                          # nothing to compare with


def test_rejected_condition_is_no_link_target(conn, demo):
    svc.reject(conn, 2, "a", "不是这个条件")
    assert "condition_ref_not_found" in _codes(svc.get_record(conn, 4))


def test_log_is_append_only(conn, demo):
    svc.approve(conn, 1, "a")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM review_log")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review_log SET reviewer='b'")


def test_list_filters(conn, demo):
    assert [r["id"] for r in svc.list_records(conn, kind="condition")] == [2, 3]
    assert [r["id"] for r in svc.list_records(conn, kind="observation", flagged=False)] == [4, 6]
    assert json.loads(json.dumps(svc.list_records(conn)[0]))["summary"].endswith("include")
