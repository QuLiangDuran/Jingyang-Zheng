import json
import sqlite3

import pytest

from audit import extract
from audit.extract import ParseError, add_document, extract_document, parse_response
from audit.fields import ai_fields
from audit.llm import ReplayClient
from tests.conftest import demo_client, load_demo

TEXT = "Methods. Strain X was grown in BG-11 at 30 °C.\n\nResults. Biomass was 1.2 g/L."


def _flags(conn, rid):
    return {f["code"] for f in json.loads(conn.execute("SELECT flags_json FROM records WHERE id=?",
                                                       (rid,)).fetchone()[0])}


# ---------- prompt ----------

def test_prompt_example_is_generated_from_fields_and_version_bumped():
    ex = extract.response_example()
    assert set(ex) == {"source", "conditions", "observations"}
    for key, level in (("source", "source"), ("conditions", "condition"), ("observations", "observation")):
        item = ex[key] if key == "source" else ex[key][0]
        names = [f.name for f in ai_fields(level)]
        assert 3 <= len(item) <= 5 and set(item) <= set(names)              # short example, real field names
        assert list(item) == [n for n in names if n in item]                # in field-list order
    assert ex["conditions"][0]["condition_key"] == "..."                     # required links are shown,
    assert ex["observations"][0]["condition_ref"] == "..."                   # as bare string labels
    assert {"value": "...", "quote": None} in ex["observations"][0].values()  # a field without a quote
    prompt = extract.build_system_prompt()
    assert json.dumps(ex, ensure_ascii=False) in prompt
    for f in ai_fields("condition") + ai_fields("observation") + ai_fields("source"):
        assert f'- "{f.name}" (' in prompt                                   # every AI field is described
    assert "use one of: growth |" in prompt and "dry_biomass" in prompt        # temporary trait / metric lists
    assert extract.PROMPT_VERSION == "v4"


def test_merged_rules_use_fields_py_names_and_decisions():
    prompt = extract.build_system_prompt()
    for text in (
        "Current focus: bio-photovoltaics (BPV).",
        "Never infer or guess a value that is not written, and do\n  not convert units.",        # D
        "parse value_numeric from value_original",                                            # D: normalization
        'Fields marked "no quote needed" are judgements and\n  take no quote.',                # B
        "If a field is not stated, omit it.",                                                  # C, E
        "Do not extract a condition on its own",                                               # R
        "Only skip a result if you cannot tell which organism AND which condition produced it.",  # S
        "identity_resolution: strain | species_only | genus_only | named_mixture | unresolved",  # H
        "strain_accession_reported: fill ONLY if the paper gives",                             # I
        "identity_resolution = named_mixture",                                                 # J
        "culture_type = mixed culture",                                                        # J
        "value_status = observation",                                                          # L
        "trait_group = bioelectrical",                                                         # M
        "potential_applied = yes | no | unknown",                                              # O
        "growth_rate -> specific_growth_rate", "biomass -> dry_biomass",                       # Q
        "exclusion_reason as one of no_original_data | no_eligible_organism | no_relevant_result | cannot_link_result",
    ):
        assert text in prompt, text
    # names from the rules file that do not exist in fields.py must not reach the model
    for old in ("leads_to_original_papers", "organism_level", "strain_code", "device_type", "applied_voltage",
                "value_type", '"organism_name"', "is_electrical", "One record per distinct result"):
        assert old not in prompt, old
    assert '- "strain_id"' not in prompt                                       # copied by the system, not the AI


def test_prompt_leaves_out_fields_the_ai_does_not_fill():
    prompt = extract.build_system_prompt()
    for name in ("study_id", "condition_id", "observation_id", "review_status", "reviewer_id",
                 "accepted_taxon_name", "availability_status", "supplier_url", "is_electrical"):
        assert f'"{name}"' not in prompt
    assert len(prompt) < 26_000  # v2 26,360; v3 20,059; v4 adds the merged rules. Guards against growth.


# ---------- parse_response ----------

def test_parse_three_levels():
    raw = json.dumps({
        "source": {"doi": {"value": "10.1/x", "quote": "10.1/x"}},
        "conditions": [{"condition_key": "c1", "genus": {"value": "Chlorella", "quote": "Chlorella"}}],
        "observations": [{"condition_ref": "c1", "value_numeric": {"value": 1.2, "quote": "1.2 g/L"}}],
    })
    p = parse_response(raw)
    assert p.missing_layers == [] and p.dropped_fields == []
    assert p.source.get("doi") == "10.1/x"
    assert p.conditions[0].get("condition_key") == "c1"
    assert p.observations[0].get("value_numeric") == "1.2"
    assert [k for k, _ in p.records()] == ["source", "condition", "observation"]


def test_parse_missing_layer_is_reported_and_keeps_data():
    p = parse_response('{"observations": [{"condition_ref": "c1", "analyte": {"value": "biomass", "quote": "x"}}]}')
    assert p.missing_layers == ["source", "conditions"]
    assert p.source is None and p.conditions == []
    assert len(p.observations) == 1 and p.observations[0].get("analyte") == "biomass"
    assert "missing layers: source, conditions" in p.notes()


def test_parse_handles_fences_and_reports_unknown_fields():
    raw = 'Sure:\n```json\n{"source": null, "conditions": [{"condition_key": "c1", "genus": "Chlorella", ' \
          '"bogus": 1}], "observations": []}\n```'
    p = parse_response(raw)
    assert p.conditions[0].get("genus") == "Chlorella"
    assert "bogus" not in p.conditions[0].fields
    assert p.dropped_fields == ["conditions[0].bogus"]


@pytest.mark.parametrize("raw", [
    "no json here",
    '{"source": {"doi": {"value": "10.1/x", "quote": "10.1',  # truncated
    '{"records": []}',                                        # old v1 shape
    '{"source": {}, "conditions": {"a": 1}}',                 # layer of the wrong type
    '{"conditions": ["not an object"]}',
])
def test_parse_rejects_incomplete_or_malformed(raw):
    with pytest.raises(ParseError):
        parse_response(raw)


# ---------- extraction ----------

def test_incomplete_json_is_an_error_and_raw_is_kept(conn):
    doc = add_document(conn, "t", TEXT, 1000)
    truncated = '{"conditions": [{"condition_key": "c1", "genus": {"value": "Strain'
    s = extract_document(conn, doc, ReplayClient([truncated]))
    assert (s.runs, s.records, s.failed_runs) == (1, 0, 1)
    run = conn.execute("SELECT status, raw_response, error FROM extraction_runs").fetchone()
    assert run["status"] == "parse_error" and run["raw_response"] == truncated
    assert "incomplete JSON" in run["error"]


def test_missing_layers_are_flagged_not_dropped(conn):
    doc = add_document(conn, "t", TEXT, 1000)
    raw = json.dumps({"observations": [{"condition_ref": "c1",
                                        "analyte": {"value": "Biomass", "quote": "Biomass was 1.2 g/L"}}]})
    extract_document(conn, doc, ReplayClient([raw]))
    rows = conn.execute("SELECT id, kind, parent_id FROM records ORDER BY id").fetchall()
    assert [r["kind"] for r in rows] == ["observation", "source"]     # empty source created by the system
    assert "condition_ref_not_found" in _flags(conn, rows[0]["id"]) and rows[0]["parent_id"] is None
    assert "layer_missing" in _flags(conn, rows[1]["id"])
    assert "missing layers" in conn.execute("SELECT error FROM extraction_runs").fetchone()[0]


def test_condition_ref_mismatch_is_flagged_and_kept(conn):
    doc = add_document(conn, "t", TEXT, 1000)
    raw = json.dumps({
        "source": {"doi": {"value": "10.1/x", "quote": None}},
        "conditions": [{"condition_key": "c30", "genus": {"value": "Strain X", "quote": "Strain X"}}],
        "observations": [{"condition_ref": "c25", "analyte": {"value": "Biomass", "quote": "Biomass was 1.2 g/L"}}],
    })
    extract_document(conn, doc, ReplayClient([raw]))
    obs = conn.execute("SELECT id, parent_id, proposed_json FROM records WHERE kind='observation'").fetchone()
    assert obs["parent_id"] is None
    assert json.loads(obs["proposed_json"])["fields"]["condition_ref"]["value"] == "c25"
    assert "condition_ref_not_found" in _flags(conn, obs["id"])
    cond = conn.execute("SELECT parent_id FROM records WHERE kind='condition'").fetchone()
    assert cond["parent_id"] == conn.execute("SELECT id FROM records WHERE kind='source'").fetchone()[0]


def test_links_resolve_across_segments(conn):
    doc = add_document(conn, "t", TEXT, 10)  # two segments
    llm = ReplayClient([
        '{"conditions": [{"condition_key": "c1", "genus": {"value": "Strain X", "quote": "Strain X"}}]}',
        '{"observations": [{"condition_ref": "c1", "analyte": {"value": "Biomass", "quote": "Biomass"}}]}',
    ])
    s = extract_document(conn, doc, llm)
    assert s.runs == 2 and s.failed_runs == 0
    cond, obs = (conn.execute("SELECT id, parent_id FROM records WHERE kind=?", (k,)).fetchone()
                 for k in ("condition", "observation"))
    assert obs["parent_id"] == cond["id"]


def test_llm_exception_recorded(conn):
    class Boom(ReplayClient):
        def complete(self, s, u):
            raise RuntimeError("down")
    doc = add_document(conn, "t", "Para one.", 100)
    s = extract_document(conn, doc, Boom([]))
    assert s.failed_runs == 1
    assert conn.execute("SELECT status FROM extraction_runs").fetchone()["status"] == "llm_error"
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0  # no placeholder without an answer


# ---------- whole paper / chunks / truncation ----------

def _parent(conn, rid):
    return conn.execute("SELECT parent_id FROM records WHERE id=?", (rid,)).fetchone()[0]


def test_whole_paper_is_one_call_and_methods_conditions_link_to_results(conn):
    llm = demo_client()
    load_demo(conn, llm=llm)
    assert len(llm.calls) == 1 and conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1
    user = llm.calls[0][1]
    assert "Methods. Chlorella" in user and "Results. After 96 h" in user and "part 1 of" not in user
    text = conn.execute("SELECT text FROM documents").fetchone()[0]
    c1 = json.loads(conn.execute("SELECT current_json FROM records WHERE id=2").fetchone()[0])["fields"]
    assert c1["condition_key"]["value"] == "control_25C"
    assert text.index(c1["medium_name"]["quote"]) < text.index("Results.")       # condition from Methods
    for obs in (4, 6):                                                           # results from Results
        o = json.loads(conn.execute("SELECT current_json FROM records WHERE id=?", (obs,)).fetchone()[0])
        assert text.index(o["fields"]["analyte"]["quote"]) > text.index("Results.")
        assert _parent(conn, obs) == 2                                           # ... same condition C1
    assert _parent(conn, 5) == _parent(conn, 8) == 3 and _parent(conn, 2) == _parent(conn, 3) == 1


def test_long_paper_is_chunked_and_later_chunks_reuse_condition_keys(conn):
    llm = demo_client()
    load_demo(conn, max_chars=600, llm=llm)  # 3 chunks: abstract / methods / results
    assert len(llm.calls) == 3
    first, second, third = (u for _, u in llm.calls)
    assert "part 1 of 3" in first and "already extracted" not in first
    assert 'return "source": null' in second
    assert '"condition_key": "control_25C"' in third and '"condition_key": "heat_35C"' in third
    assert [_parent(conn, i) for i in range(1, 9)] == [None, 1, 1, 2, 3, 2, None, 3]  # same links as whole paper


def test_source_from_a_later_chunk_is_not_a_second_paper(conn):
    doc = add_document(conn, "t", TEXT, 10)  # two chunks
    src = {"doi": {"value": "10.1/x", "quote": None}}
    s = extract_document(conn, doc, ReplayClient([json.dumps({"source": src}), json.dumps({"source": src})]))
    assert s.failed_runs == 0
    assert conn.execute("SELECT COUNT(*) FROM records WHERE kind='source'").fetchone()[0] == 1
    run2 = conn.execute("SELECT error, raw_response FROM extraction_runs ORDER BY id DESC").fetchone()
    assert "already taken from part 1" in run2["error"] and "10.1/x" in run2["raw_response"]


def test_truncated_output_is_a_parse_error_and_not_continued(conn):
    class Cut(ReplayClient):
        def complete(self, s, u):
            out = super().complete(s, u)
            self.last_finish_reason = "length"
            return out
    doc = add_document(conn, "t", TEXT, 200_000)
    raw = '{"source": {"doi": {"value": "10.1/x", "quote": null}}, "observations": [{"condition_ref": "c1"'
    llm = Cut([raw])
    s = extract_document(conn, doc, llm)
    assert len(llm.calls) == 1 and (s.runs, s.records, s.failed_runs) == (1, 0, 1)
    run = conn.execute("SELECT status, error, raw_response FROM extraction_runs").fetchone()
    assert run["status"] == "parse_error" and "输出被截断" in run["error"] and run["raw_response"] == raw


def test_trait_group_and_metric_outside_temporary_list_is_a_warning(conn, demo):
    from audit import review_service as svc
    r = svc.edit_field(conn, 4, "a", "metric_code", "x", value="biomass_weird")
    [f] = [f for f in r["flags"] if f["code"] == "not_in_temp_list"]
    assert (f["field"], f["level"]) == ("metric_code", "warning")
    r = svc.edit_field(conn, 4, "a", "trait_group", "x", value="mystery")
    assert {(f["field"], f["level"]) for f in r["flags"]} == {("metric_code", "warning"), ("trait_group", "warning")}
    r = svc.edit_field(conn, 4, "a", "metric_code", "x", value="dry_biomass")
    assert "metric_code" not in {f["field"] for f in r["flags"]}


def test_demo_flags(conn, demo):
    rows = conn.execute("SELECT id, kind, parent_id FROM records ORDER BY id").fetchall()
    assert [r["kind"] for r in rows] == ["source"] + ["condition"] * 2 + ["observation"] * 5
    assert [r["parent_id"] for r in rows] == [None, 1, 1, 2, 3, 2, None, 3]
    codes = [_flags(conn, r["id"]) for r in rows]
    assert codes[0] == set()
    assert codes[1] == {"missing_vs_sibling"}                          # C2 has phase_duration, C1 not
    assert codes[2] == {"choice_not_allowed", "quote_not_found",       # "lake water"; paraphrased quote
                        "missing_vs_sibling"}                          # C1 has light, photoperiod, ...
    assert codes[3] == set() and codes[5] == set()
    # 0.79 vs 0.97; repeated in Discussion
    assert codes[4] == {"value_not_in_quote", "value_numeric_mismatch", "duplicate_observation"}
    assert codes[6] == {"condition_ref_not_found"}                     # heat_30C does not exist
    assert codes[7] == codes[4]


def test_proposed_and_raw_are_immutable(conn, demo):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE records SET proposed_json='{}' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE extraction_runs SET raw_response='x' WHERE id=1")
