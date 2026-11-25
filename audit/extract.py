"""Whole-paper AI extraction. Raw responses are always stored before parsing.

The whole paper goes into one call. Only a paper longer than MAX_DOC_CHARS is split
into chunks (at paragraph boundaries); each later chunk is told which conditions were
already extracted so it reuses their condition_key, and the paper-level record is
taken from the first chunk only. Links are resolved document-wide afterwards.

The AI returns three levels: {"source": {...}, "conditions": [...], "observations": [...]}.
Field lists and the JSON example in the prompt are generated from fields.py.
"""
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .db import now
from .documents import segment_text
from .fields import FIELDS_BY_NAME, QUOTE_FROM, SCHEMA_VERSION, TRAIT_TO_METRIC, FieldValue, RecordContent, ai_fields
from .llm import LLMClient
from .review_service import refresh_document

PROMPT_VERSION = "v11"  # v11: figure points as seen, metric/unit rules, observations first, reference groups (PLAN #38); v10: v10: observations-only default quote, full trait coverage, verbatim organism (PLAN #37); v9: several templates per condition, default quotes, series/specific/reference/unit/duplicate rules (PLAN #36); v8: compact output (PLAN #33)

# JSON key of each level in the AI response.
LAYER_KEYS = {"source": "source", "condition": "conditions", "observation": "observations"}
LEVEL_NAMES = {"source": "the paper (one object)",
               "condition": "one organism under one treatment and stage, with its cultivation and electrical setup",
               "observation": "one measured quantity at one time under one condition"}


def _field_line(f) -> str:
    bits = [f.type]
    if f.essential:
        bits.append("required for electrical data" if f.electrical_only else "required")
    if f.choices:
        bits.append("one of: " + " | ".join(f.choices + ["other", "unspecified"]))
    if f.suggested:  # metric_code is never "other" (v11): a descriptive code is written instead
        bits.append("use one of: " + " | ".join(f.suggested + ([] if f.name == "metric_code" else ["other"])))
    if f.filled_by == "link":
        bits.append("plain string label")
    elif f.name in QUOTE_FROM:
        bits.append(f"no quote needed: the quote of {QUOTE_FROM[f.name]} is used")
    elif not f.needs_quote:
        bits.append("no quote needed")
    return f'- "{f.name}" ({", ".join(bits)}): {f.description}'


def _example_fields(level: str) -> list:
    """A few representative fields per level (the full key list is in the field lines):
    required links, two fields with quotes (required first) and one field without a quote."""
    fs = ai_fields(level)
    links = [f for f in fs if f.filled_by == "link" and f.essential]
    quoted = sorted((f for f in fs if f.filled_by == "ai" and f.needs_quote), key=lambda f: not f.essential)[:2]
    unquoted = sorted((f for f in fs if f.filled_by == "ai" and not f.needs_quote), key=lambda f: not f.essential)[:1]
    chosen = {f.name for f in links + quoted + unquoted}
    return [f for f in fs if f.name in chosen]  # keep field-list order


def _example_item(level: str) -> dict:
    """v8 compact form: a quoted field is ["value", "qN"], any other field a bare value."""
    item: dict = {}
    for f in _example_fields(level):
        if f.filled_by == "link" or not f.needs_quote:
            item[f.name] = "..."
        else:
            item[f.name] = ["...", "q1"]
    item["not_stated"] = ["..."]
    return item


def response_example() -> dict:
    """The response shape shown to the AI, generated from fields.py."""
    cond = _example_item("condition")
    template = {k: v for k, v in cond.items() if k not in ("condition_key", "not_stated")}
    return {"quotes": {"q1": "...", "q2": "..."},
            "source": _example_item("source"),
            "observations": [{"quote": "q1", **_example_item("observation"),
                              "extraction_method": "...", "value_status": "..."}],
            "condition_templates": {"T1": template, "T2": {"condition_source_locator": "..."}},
            "conditions": [{"condition_key": "...", "template": ["T1", "T2"], "not_stated": ["..."]}]}


def empty_response() -> dict:
    return {"source": None, "conditions": [], "observations": []}


def _options(name: str) -> str:
    return " | ".join(FIELDS_BY_NAME[name].choices)


PDF_RULES = """
=== ORIGINAL PDF ATTACHED ===
The original PDF of this paper is attached. The "Text" in the user message was extracted from that
PDF and is the reference for every quote.
- Every "quote" MUST still be copied verbatim from the Text, never retyped from the PDF pages.
- Use the PDF pages for what the Text lacks: figures, and table layout that the Text lost.
- A value stated in the Text: take it from the Text (extraction_method = text or table).
- Figure points: record as many observations as points you can see in the panel; never add a point,
  never fill in a point between others. For each point, read the x value against the axis as an
  approximate value (e.g. "~3.8"), with the unit exactly as the axis writes it; never assume even spacing,
  and never take the x values from a list in the Text unless the Text ties each value to its point.
- A value shown ONLY in a figure: read it from the figure only if you can read it clearly against
  the axis. Then set extraction_method = graph_estimated, value_status = approximate, put the value
  you read in value_original and value_numeric, source_locator = the figure panel (e.g. "Figure 3a"),
  and quote the figure caption or the Text sentence that refers to that figure.
- A figure value you cannot read reliably: extraction_method = graph_only, value_status = graph-only,
  leave the value empty, still fill source_locator and the other fields.
- Do not report the same result twice (once from the Text and once from a figure).
"""


PAGE_IMAGES_RULES = """
=== PAGE IMAGES ===
The PDF is attached as images of its pages, each preceded by a label such as [PDF page 5]. Only
pages with figures or tables may be included; the rest of the paper is in the Text. In
source_locator give the figure or table (e.g. "Figure 3a"), not the page label.
"""


def build_system_prompt(with_pdf: bool = False, pdf_mode: str | None = None) -> str:
    """Rules merged from docs/extraction_rules_prompt.txt (field names mapped to fields.py,
    decisions of 2026-11-25); every option list and field name comes from fields.py."""
    field_lines = "\n\n".join(
        f'"{LAYER_KEYS[lv]}" -- {LEVEL_NAMES[lv]}:\n' + "\n".join(_field_line(f) for f in ai_fields(lv))
        for lv in LAYER_KEYS)
    trait_map = ", ".join(f"{t} -> {' / '.join(ms)}" for t, ms in TRAIT_TO_METRIC.items())
    return f"""You are extracting records for the MicroalgaeMatch database from the text of ONE scientific paper.
Current focus: bio-photovoltaics (BPV).

=== TWO SEPARATE QUESTIONS ===
1. PAPER SELECTION (paper_verdict): does this paper qualify for the BPV dataset? It qualifies only
   if it reports at least one electrical result from an eligible living organism.
2. EXTRACTION SCOPE (conditions and observations): for original research, extract EVERY measured
   trait the paper reports for eligible organisms, together with its conditions -- growth, biomass,
   biomass composition, pigments and chlorophyll, photosynthesis, morphology, stress tolerance,
   electrical output and any other measured trait. The electrical criterion is ONLY for selection:
   never leave out a result because it is not electrical, and extract the observations whatever
   the paper_verdict is.

Fields of each record:
{field_lines}

=== RECORD STRUCTURE ===
- Return three levels: "source" (the paper), "conditions" and "observations".
- One observation = one measured trait (one quantity at one time). It links to exactly one
  condition: its "condition_ref" is that condition's "condition_key".
- One condition = one organism + one set of conditions (cultivation and, for electrical
  results, the device setup).
- Do not extract a condition on its own: only conditions that at least one observation links to.
- Settings shared by several conditions (organism, strain, medium, cultivation, device, electrodes,
  mediator, ...) go ONCE into "condition_templates": {{"T1": {{...}}, "T2": {{...}}}}, e.g. one template
  for the organism and its cultivation and one per device setup. A condition lists ALL templates it
  uses, in order: "template": ["T1", "T2"] (a later one wins on the same field), and then only the
  fields that differ (group_type, the varied setting, ...). A field written in a condition replaces
  the templates'; null removes it. Every condition must end up with its organism: an electrical
  condition names the organism's template too. Put condition_source_locator in the template.
- Do not create two identical conditions: if two conditions would have the same templates and the
  same settings, they are one condition.
- If you can tell which organism produced a result but not all of its conditions, still extract
  it: create a condition with the organism and whatever is stated, and link the observation to it.
  Only skip a result if you cannot tell which organism AND which condition produced it.

=== VALUES, QUOTES, MISSING DATA ===
- Extract only what the paper states. Never infer or guess a value that is not written, and do
  not convert units.
- Allowed format normalization: parse value_numeric from value_original; map the paper's wording
  to one of the listed options; split a light:dark cycle such as 16:8 into light_hours and dark_hours.
- Every field you fill that needs a quote is written ["value", "qN"]: qN is the id of the span
  that shows the value, and "quotes" maps each id to that span copied verbatim from the Text (a
  short span, ideally one sentence or less). Write each span ONCE in "quotes" and reuse its id for
  every field and every record it supports. Fields marked "no quote needed" are judgements and
  take no quote: write them as a bare value. value_original and unit_original ALWAYS take a quote id.
- Only an observation may give a default "quote": "qN": every field in it written as a bare value
  (or ["value"]) then takes that quote; use it when one span shows the whole result. Templates and
  conditions have no default: every field of a template or condition takes its own quote id
  (numbers too), because a template gathers many Methods sentences.
- organism_name_reported is copied exactly as written, e.g. "P. tricornutum": never expand an
  abbreviated name.
- value_numeric must be a number written in value_original, exactly as written: never convert units
  ("~100 nA/cm2" gives value_numeric 100, not 0.1).
- A quote (and evidence_excerpt) is ONE continuous span of the Text, copied character for character,
  brackets and abbreviations included: "Indium Tin Oxide (ITO)-conductive glass", not "ITO-conductive
  glass". Never join pieces from different places into one quote; quote the one place that shows it.
- If a field is not stated, omit it. Missing details do NOT remove a record.
- "not_stated" (a list in every record): the REQUIRED fields of that record that you looked for and
  the paper does not give, e.g. ["feed_mode"]. Never fill them with "unspecified" or a guess instead.
  A required field that is neither filled nor listed in not_stated counts as missed by you.
- mediator = none ONLY if the paper says no mediator was added. If it does not say, list mediator
  in not_stated; do not conclude it from the setup description.

=== PAPER-LEVEL CHECK (do this first) ===
- paper_type: {_options("paper_type")} | other.
- Extract conditions and observations ONLY from original research (the authors collected the
  data themselves). For a review, editorial or pure computer model, return only "source".

=== ORGANISM RULES ===
- organism_name_reported: the name exactly as written in the paper.
- identity_resolution: {_options("identity_resolution")}.
- strain_accession_reported: fill ONLY if the paper gives a strain or collection code, copied
  exactly. Do NOT guess a strain code. Do NOT apply a species-level result to a specific strain
  or to every strain of that species.
- If the result comes from a NAMED mixture of organisms, make ONE condition with
  identity_resolution = named_mixture, the members as written in organism_name_reported, and
  culture_type = mixed culture. Do not split it into one condition per member.
- Do NOT extract: seaweed/macroalgae, land plants, purified pigments, isolated cell parts
  (e.g. thylakoids, photosystems), or unidentified mixtures that cannot be tied to a named
  organism or defined mixture.
- If a paper has both eligible and ineligible experiments, extract only the eligible ones.

=== TRAIT RULES ===
- Extract every measured trait stated in the Text (Results and Discussion), not only values in
  figures, tables and series: e.g. detachment ratios, chloroplast counts, viability (FDA) values.
  The series rule below adds observations; it never replaces a trait stated in a sentence.
- Extract every measured trait, not only the examples below. Report each value the paper states
  separately (each group, each time point, each organism) as its own observation.
- trait_group and metric_code: use the values listed in the field descriptions above.
  Examples of common traits and their metric_code (examples, NOT a limit): {trait_map}.
- If no listed metric_code fits a measured trait, still extract it: write a short descriptive
  snake_case metric_code (e.g. nitrate_uptake_rate) and the closest trait_group; the system
  flags it for the reviewer. metric_code is never other or unspecified.
- The unit decides the metric: a unit of power (W, mW, µW) is a power (power, max_power), a unit
  per area (mW/m2, µA/cm2) is a density (power_density, max_power_density, current_density, ...).
- Every observation states extraction_method and value_status, even when the value is plain text.
- A relative difference (e.g. "10–30% higher than WT"): value_original is copied verbatim
  ("10–30%"), the comparison goes in comparison_description, and metric_code is NOT the absolute
  quantity (not photocurrent_density): write a descriptive code for the relative change.
- A clearly described non-numeric observation counts too: put it in value_category and set
  value_status = observation.
- Tables: every value cell of a results table is its own observation (one per row and measured
  column, e.g. Voc and Isc of each cell). Rows of a table appear in the Text as "label | v1 | v2 ...".
- One sentence that states a value for several organisms or groups ("1 for Fp or Pht", "Tw and Cc
  showed <2.5%") gives one observation for EACH of them; they share the same quote id.
- Settings the authors chose (a concentration, light intensity, load, potential, cell density,
  including the "optimal" one found in a series) belong to the condition, never to an observation's
  value. The unit_original of an observation must fit its metric_code: a current density is never
  in μM or mW cm-2.
- A series (a curve or a set of bars over light intensity, concentration, cell density, time, ...):
  EACH point of a series is its own observation linked to its own condition, which names the same
  templates and writes the varied setting (e.g. "assay_light_intensity": 10). If you cannot tell which
  x value a point belongs to, do not read that figure: say so in uncertain_reason instead.
- When the paper gives a value for one organism or group specifically and also a general statement
  ("fewer than 1% for Pht" and "lower than 2.5% for all species"), use the sentence specific to it.
- Resolve references: "the same result", "similar values", "as above" are written as the value they
  refer to (e.g. no photocurrent), quoting the sentence that states it.
- Values with <, >, ≤ or ≥ ("<2.5"): copy value_original verbatim and leave value_numeric empty.
  "~" or "about" values keep their number in value_numeric and value_status = approximate.
- Electrical results (voltage, current, power density and other electrical outputs):
  trait_group = bioelectrical. Chlorophyll or photosynthesis measurements are NOT bioelectrical.
- Do NOT extract from: cultivation-only descriptions, growth recipes, device descriptions without
  a measured trait, or algae used only as dead biomass or fuel without living cells being part of
  the electrical system.

=== CONDITION RULES ===
- Growth results: medium_name, temperature (and other treatment details given).
- Electrical results: system_type, and potential_applied = {_options("potential_applied")}
  (unknown = the paper does not say), plus applied_potential_value and applied_potential_unit if given.
- group_type: which group or treatment the result belongs to (e.g. control vs. treated).
- A reference group (e.g. "single µPSC", "bare electrode", "wild type") is its own condition: never
  link its values to one specific numbered unit or strain of the tested groups.
- A value that appears in the Text and in a table (or a figure) is ONE observation.
- Do not carry device settings from one mode, setup or experiment to another (e.g. the electrode
  material of a biofilm setup to a suspension setup) unless the paper says the setting is shared.
  Otherwise leave it empty and list it in not_stated if it is required.

=== PAPER VERDICT ===
paper_verdict: {_options("paper_verdict")}.
- include: at least one eligible observation AND (for the BPV focus) at least one electrical one.
  The verdict does not change what you extract (see TWO SEPARATE QUESTIONS).
- exclude: give exclusion_reason as one of {_options("exclusion_reason")}.
- uncertain: the needed information is probably in figures, supplements or another document you
  were not given. Say what is missing in uncertain_reason.

=== OUTPUT ===
- If there are no records, return {json.dumps(empty_response())}.
- Respond with JSON only: no commentary, no explanations, no plan, no reasoning in the output.
  Write "quotes" first, then "source", then the observations, then "condition_templates" and
  "conditions". Write the observations first, then only the conditions they link to.
  Shape (only a few fields are shown; use the field names listed above as keys):
{json.dumps(response_example(), ensure_ascii=False)}""" + (PDF_RULES if with_pdf else "") + (
        PAGE_IMAGES_RULES if with_pdf and pdf_mode == "images" else "")


def build_user_prompt(text: str, chunk: tuple[int, int] | None = None,
                      known_conditions: list[dict] | None = None) -> str:
    """`chunk` = (index, total) when a long paper is split. Later chunks are told that the
    paper-level record is already taken and which conditions exist, so observations can link."""
    if not chunk or chunk[1] == 1:
        return f"Text:\n<<<\n{text}\n>>>"
    i, n = chunk
    head = f"This is part {i + 1} of {n} of one paper."
    if i > 0:
        head += ' The paper-level record was taken from part 1: return "source": null.'
        if known_conditions:
            head += (" Conditions already extracted from earlier parts are listed below. Reuse their"
                     " condition_key when an observation belongs to one of them, and do not repeat them"
                     ' in "conditions"; only add conditions that are new.\n'
                     + json.dumps(known_conditions, ensure_ascii=False, indent=1))
    return f"{head}\n\nText:\n<<<\n{text}\n>>>"


def _known_conditions(conn: sqlite3.Connection, document_id: int) -> list[dict]:
    out = []
    for r in conn.execute("SELECT current_json FROM records WHERE document_id=? AND kind='condition' "
                          "AND status != 'rejected' ORDER BY id", (document_id,)):
        c = RecordContent.model_validate_json(r["current_json"])
        item = {k: c.get(k) for k in ("condition_key", "organism_name_reported", "strain_accession_reported", "group_type",
                                      "temperature", "condition_phase") if c.get(k)}
        if item.get("condition_key"):
            out.append(item)
    return out


class ParseError(ValueError):
    pass


@dataclass
class ParsedResponse:
    source: RecordContent | None = None
    conditions: list[RecordContent] = field(default_factory=list)
    observations: list[RecordContent] = field(default_factory=list)
    missing_layers: list[str] = field(default_factory=list)   # response keys that were absent
    dropped_fields: list[str] = field(default_factory=list)   # "conditions[0].bogus" -- kept in raw response

    def records(self) -> list[tuple[str, RecordContent]]:
        out = [("source", self.source)] if self.source else []
        return out + [("condition", c) for c in self.conditions] + [("observation", o) for o in self.observations]

    def notes(self) -> str | None:
        parts = []
        if self.missing_layers:
            parts.append("missing layers: " + ", ".join(self.missing_layers))
        if self.dropped_fields:
            parts.append("dropped (still in raw response): " + ", ".join(self.dropped_fields))
        return "; ".join(parts) or None


def _to_content(level: str, obj, where: str, dropped: list[str]) -> RecordContent | None:
    if not isinstance(obj, dict):
        raise ParseError(f"{where} is not an object")
    names = {f.name for f in ai_fields(level)}
    content = RecordContent.empty(level)
    for k, fv in obj.items():
        if k == "_from_template":
            content.from_template = {n: t for n, t in (fv or {}).items() if n in names}
            continue
        if k == "not_stated":
            items = fv if isinstance(fv, list) else [fv]
            for n in items:
                if isinstance(n, str) and n in names:
                    content.not_stated.append(n)
                elif n is not None:
                    dropped.append(f"{where}.not_stated.{n}")
            continue
        if k not in names:
            dropped.append(f"{where}.{k}")
            continue
        if fv is None:
            continue
        if not isinstance(fv, dict):  # tolerate bare values (link labels are bare strings)
            fv = {"value": fv, "quote": None}
        content.fields[k] = FieldValue(
            value=None if fv.get("value") is None else str(fv.get("value")),
            quote=None if fv.get("quote") is None else str(fv.get("quote")),
        )
    return content if any(v.value for v in content.fields.values()) else None


QUOTE_ID = re.compile(r"q\d+$")


def _expand_value(v, quotes: dict, where: str, dropped: list[str], default: str | None = None):
    """v8 compact field -> {"value", "quote"}. ["value", "qN"] looks the quote up in "quotes";
    ["value", "text"] is taken as a literal quote; a dict is the older long form; anything else
    is a bare value. v9: a bare value (or ["value"]) takes `default`, the resolved default quote
    of its record or template, when given."""
    if isinstance(v, list):
        if not v:
            return None
        value = v[0]
        q = v[1] if len(v) > 1 else None
        if isinstance(q, str) and q in quotes:
            q = quotes[q]
        elif isinstance(q, str) and QUOTE_ID.match(q):
            dropped.append(f"{where} (unknown quote id {q})")
            q = None
        return {"value": value, "quote": q if q is not None else default}
    if isinstance(v, dict):
        return v
    return {"value": v, "quote": default}


def expand_compact(data: dict, dropped: list[str]) -> dict:
    """Turn a v8 answer (quotes table, condition templates, compact fields) into the long form
    that _to_content reads. An older long-form answer passes through unchanged."""
    quotes = data.get("quotes") or {}
    if not isinstance(quotes, dict):
        dropped.append("quotes (not an object)")
        quotes = {}
    quotes = {str(k): str(v) for k, v in quotes.items() if v is not None}
    templates = data.get("condition_templates") or {}
    if not isinstance(templates, dict):
        dropped.append("condition_templates (not an object)")
        templates = {}

    def default_quote(obj, where):
        """v9: a record or template may give "quote": "qN" for its fields without their own id."""
        q = obj.get("quote")
        if not isinstance(q, str):
            return None
        if q in quotes:
            return quotes[q]
        if QUOTE_ID.match(q):
            dropped.append(f"{where}.quote (unknown quote id {q})")
            return None
        return q  # a literal span

    def item(obj, where, inherited=None, allow_default=False):
        """Expand one record. `inherited` = fields already expanded (from templates), kept as they are.
        v10: only observations may use a default quote; templates summarise many Methods sentences."""
        if not isinstance(obj, dict):
            return obj
        default = default_quote(obj, where) if allow_default else None
        if not allow_default and obj.get("quote") is not None:
            dropped.append(f"{where}.quote (default quote is only allowed on observations)")
        out = dict(inherited or {})
        for k, v in obj.items():
            if k == "quote":
                continue
            if k in ("not_stated", "template"):
                out[k] = v
            elif v is None:
                out[k] = None
            else:
                spec = FIELDS_BY_NAME.get(k)
                d = default if spec is not None and spec.needs_quote else None
                out[k] = _expand_value(v, quotes, f"{where}.{k}", dropped, d)
        return out

    def condition(obj, where):
        if not isinstance(obj, dict):
            return obj
        names = obj.get("template")
        names = [names] if isinstance(names, str) else [n for n in (names or []) if isinstance(n, str)]
        merged, ns, origin = {}, [], {}
        for name in names:  # v9: several templates, merged in order (a later one wins)
            t = templates.get(name)
            if not isinstance(t, dict):
                dropped.append(f"{where}.template (unknown template {name})")
                continue
            merged = item({k: v for k, v in t.items() if k != "not_stated"}, f"condition_templates.{name}", merged)
            origin.update({k: name for k, v in t.items() if k not in ("not_stated", "quote") and v is not None})
            ns += [n for n in (t.get("not_stated") or []) if n not in ns]
        own = {k: v for k, v in obj.items() if k not in ("template", "not_stated")}
        merged = item(own, where, merged)
        origin = {k: t for k, t in origin.items() if k not in own}  # v11: a field the condition sets is its own
        ns += [n for n in (obj.get("not_stated") or []) if n not in ns]
        merged = {k: v for k, v in merged.items() if v is not None}
        if ns:
            merged["not_stated"] = ns
        origin = {k: t for k, t in origin.items() if k in merged}
        if origin:
            merged["_from_template"] = origin
        return merged

    out = dict(data)
    if data.get("source") is not None:
        out["source"] = item(data["source"], "source")
    if isinstance(data.get("conditions"), list):
        out["conditions"] = [condition(c, f"conditions[{i}]") for i, c in enumerate(data["conditions"])]
    if isinstance(data.get("observations"), list):
        out["observations"] = [item(o, f"observations[{i}]", allow_default=True)
                               for i, o in enumerate(data["observations"])]
    return out


def parse_response(raw: str) -> ParsedResponse:
    s = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    if not s.startswith("{"):
        a, b = s.find("{"), s.rfind("}")
        if a == -1 or b <= a:
            raise ParseError("no JSON object in response")
        s = s[a:b + 1]
    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        raise ParseError(f"invalid or incomplete JSON: {e}") from e
    if not isinstance(data, dict):
        raise ParseError("expected a JSON object")
    if not any(k in data for k in LAYER_KEYS.values()):
        raise ParseError('expected {"source": ..., "conditions": [...], "observations": [...]}')

    out = ParsedResponse()
    out.missing_layers = [k for k in LAYER_KEYS.values() if k not in data]
    data = expand_compact(data, out.dropped_fields)
    if data.get("source") is not None:
        out.source = _to_content("source", data["source"], "source", out.dropped_fields)
    for level in ("condition", "observation"):
        key = LAYER_KEYS[level]
        items = data.get(key) or []
        if not isinstance(items, list):
            raise ParseError(f'"{key}" must be a list')
        parsed = [_to_content(level, it, f"{key}[{i}]", out.dropped_fields) for i, it in enumerate(items)]
        setattr(out, key, [p for p in parsed if p is not None])
    return out


@dataclass
class ExtractSummary:
    document_id: int
    runs: int = 0
    records: int = 0
    failed_runs: int = 0
    calls: int = 0              # API calls, continuations included
    completion_tokens: int = 0
    errors: list = field(default_factory=list)


def _pdf_for(source_path: str | None) -> str | None:
    """The original PDF of a document, if it was imported from one (or with --pdf) and still exists."""
    if source_path and source_path.lower().endswith(".pdf") and Path(source_path).is_file():
        return source_path
    return None


def add_document(conn: sqlite3.Connection, title: str, text: str, max_chars: int,
                 source_path: str | None = None) -> int:
    with conn:
        doc_id = conn.execute(
            "INSERT INTO documents (title, source_path, text, created_at) VALUES (?,?,?,?)",
            (title, source_path, text, now())).lastrowid
        for seg in segment_text(text, max_chars):
            conn.execute('INSERT INTO segments (document_id, idx, start, "end") VALUES (?,?,?,?)',
                         (doc_id, seg.idx, seg.start, seg.end))
    return doc_id


def delete_document(conn: sqlite3.Connection, document_id: int) -> dict:
    """Delete a document with its segments, runs and records. Refused once anyone has reviewed
    one of its records: the review log is append-only and must keep pointing at real records."""
    if conn.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone() is None:
        raise ValueError(f"document {document_id} not found")
    reviewed = conn.execute(
        "SELECT COUNT(*) FROM review_log l JOIN records r ON r.id = l.record_id WHERE r.document_id=?",
        (document_id,)).fetchone()[0]
    if reviewed:
        raise ValueError(f"文档 {document_id} 已有 {reviewed} 条审核记录，不能删除")
    counts = {}
    with conn:
        # one statement per table: SQLite checks the records.parent_id self-reference at statement end
        for table, col in (("records", "document_id"), ("extraction_runs", "document_id"),
                           ("segments", "document_id"), ("documents", "id")):
            counts[table] = conn.execute(f"DELETE FROM {table} WHERE {col}=?", (document_id,)).rowcount
    return counts


def _insert_record(conn, run_id, document_id, segment_id, kind, content: RecordContent) -> int:
    js = content.model_dump_json()
    return conn.execute(
        """INSERT INTO records (run_id, document_id, segment_id, kind, proposed_json, current_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (run_id, document_id, segment_id, kind, js, js, now())).lastrowid


def extract_document(conn: sqlite3.Connection, document_id: int, llm: LLMClient) -> ExtractSummary:
    doc = conn.execute("SELECT text, source_path FROM documents WHERE id=?", (document_id,)).fetchone()
    if doc is None:
        raise ValueError(f"document {document_id} not found")
    text = doc["text"]
    pdf = _pdf_for(doc["source_path"]) if getattr(llm, "supports_pdf", False) else None
    system = build_system_prompt(with_pdf=pdf is not None, pdf_mode=getattr(llm, "pdf_input", None))
    summary = ExtractSummary(document_id)
    last_ok: tuple[int, int] | None = None  # (run_id, segment_id)

    segments = conn.execute('SELECT id, idx, start, "end" FROM segments WHERE document_id=? ORDER BY idx',
                            (document_id,)).fetchall()
    for seg in segments:
        chunk = (seg["idx"], len(segments))
        user = build_user_prompt(text[seg["start"]:seg["end"]], chunk,
                                 _known_conditions(conn, document_id) if seg["idx"] > 0 else None)
        prompt = f"[system]\n{system}\n\n[user]\n{user}"
        if pdf:
            prompt = f"[attached pdf]\n{pdf}\n\n" + prompt
        raw, status, error, parsed = None, "ok", None, ParsedResponse()
        try:
            if hasattr(llm, "last_finish_reason"):
                llm.last_finish_reason = None
            if hasattr(llm, "last_usage"):
                llm.last_usage = None
            raw = llm.complete(system, user, pdf_path=pdf) if pdf else llm.complete(system, user)
            if pdf and getattr(llm, "last_pdf_ref", None):
                prompt = prompt.replace(f"[attached pdf]\n{pdf}", f"[attached pdf]\n{pdf} ({llm.last_pdf_ref})", 1)
            if getattr(llm, "last_finish_reason", None) == "length":
                usage = getattr(llm, "last_usage", None) or {}
                calls = usage.get("calls") or []
                raise ParseError(
                    f"输出被截断（finish_reason=length）：共 {len(calls)} 次调用（含自动续写），"
                    f"每次输出 token {[c.get('completion_tokens') for c in calls]}，"
                    f"max_tokens={usage.get('max_tokens')}。如果每次都停在同一个数且小于 max_tokens，"
                    "就是模型自身的输出上限；可调大 LLM_MAX_CONTINUATIONS，或调小 MAX_DOC_CHARS 分块提取。"
                    "原始回答已保存")
            parsed = parse_response(raw)
            if parsed.source and seg["idx"] > 0 and conn.execute(
                    "SELECT 1 FROM records WHERE document_id=? AND kind='source'", (document_id,)).fetchone():
                parsed.source = None
                parsed.dropped_fields.append("source (paper-level record already taken from part 1)")
            error = parsed.notes()
        except ParseError as e:
            status, error = "parse_error", str(e)
        except Exception as e:  # network / auth / provider errors
            status, error = "llm_error", f"{type(e).__name__}: {e}"

        records = parsed.records()
        usage = getattr(llm, "last_usage", None)
        usage_json = json.dumps(usage) if usage else None
        if usage:
            summary.completion_tokens += usage.get("completion_tokens") or 0
            summary.calls += len(usage.get("calls") or [])
        if error and status != "ok":
            summary.errors.append(error)
        with conn:
            run_id = conn.execute(
                """INSERT INTO extraction_runs (document_id, segment_id, model, base_url, prompt_version,
                   schema_version, temperature, prompt, raw_response, status, error, created_at, usage)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (document_id, seg["id"], llm.model, llm.base_url, PROMPT_VERSION, SCHEMA_VERSION,
                 llm.temperature, prompt, raw, status, error, now(), usage_json)).lastrowid
            for kind, content in records:
                _insert_record(conn, run_id, document_id, seg["id"], kind, content)
        if status == "ok":
            last_ok = (run_id, seg["id"])
        summary.runs += 1
        summary.records += len(records)
        summary.failed_runs += status != "ok"

    if pdf and hasattr(llm, "cleanup"):
        llm.cleanup()
    with conn:
        has_source = conn.execute("SELECT 1 FROM records WHERE document_id=? AND kind='source'",
                                  (document_id,)).fetchone()
        if not has_source and last_ok:
            # The AI returned no paper-level record. Create an empty one so conditions have a
            # parent and the reviewer has somewhere to fill paper fields; it is flagged.
            _insert_record(conn, last_ok[0], document_id, last_ok[1], "source", RecordContent.empty("source"))
        refresh_document(conn, document_id)
    return summary


def reparse_document(conn: sqlite3.Connection, document_id: int) -> ExtractSummary:
    """Re-parse failed runs from their stored raw answers, with continuation seams repaired.
    No API call. The old run is kept untouched; a new run (same model, prompt and versions, raw =
    the repaired answer) holds the records. Segments that already have records are skipped."""
    from .llm import repair_seams

    summary = ExtractSummary(document_id)
    runs = conn.execute(
        """SELECT * FROM extraction_runs WHERE id IN (
               SELECT MAX(id) FROM extraction_runs WHERE document_id=? GROUP BY segment_id)
           ORDER BY segment_id""", (document_id,)).fetchall()
    if not runs:
        raise ValueError(f"document {document_id} has no extraction runs")
    last_ok = None
    for run in runs:
        seg = conn.execute("SELECT id, idx FROM segments WHERE id=?", (run["segment_id"],)).fetchone()
        if run["status"] == "ok" or not run["raw_response"]:
            summary.errors.append(f"segment {seg['idx']}: run {run['id']} is {run['status']}"
                                  f"{'' if run['raw_response'] else ' without a stored answer'}; skipped")
            continue
        if conn.execute("SELECT 1 FROM records WHERE document_id=? AND segment_id=?",
                        (document_id, seg["id"])).fetchone():
            summary.errors.append(f"segment {seg['idx']}: already has records; skipped")
            continue
        usage = json.loads(run["usage"]) if run["usage"] else {}
        seams = usage.get("seams")
        if seams is None:  # runs from before seams were stored: cumulative lengths of the pieces
            lens = [c.get("chars") or 0 for c in usage.get("calls") or []]
            seams = [sum(lens[:i + 1]) for i in range(len(lens) - 1)]
        text, notes = repair_seams(run["raw_response"], seams)
        status, error, parsed = "ok", None, ParsedResponse()
        try:
            parsed = parse_response(text)
            if parsed.source and seg["idx"] > 0 and conn.execute(
                    "SELECT 1 FROM records WHERE document_id=? AND kind='source'", (document_id,)).fetchone():
                parsed.source = None
            error = parsed.notes()
        except ParseError as e:
            status, error = "parse_error", str(e)
        note = f"reparsed from run {run['id']} without an API call; seam repairs: {json.dumps(notes, ensure_ascii=False)}"
        error = f"{note}\n{error}" if error else note
        records = parsed.records()
        with conn:
            run_id = conn.execute(
                """INSERT INTO extraction_runs (document_id, segment_id, model, base_url, prompt_version,
                   schema_version, temperature, prompt, raw_response, status, error, created_at, usage)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (document_id, seg["id"], run["model"], run["base_url"], run["prompt_version"],
                 run["schema_version"], run["temperature"], run["prompt"], text, status, error, now(),
                 json.dumps({**usage, "reparsed_from_run": run["id"], "seam_repairs": notes}))).lastrowid
            for kind, content in records:
                _insert_record(conn, run_id, document_id, seg["id"], kind, content)
        summary.runs += 1
        summary.records += len(records)
        summary.failed_runs += status != "ok"
        if status == "ok":
            last_ok = (run_id, seg["id"])
        else:
            summary.errors.append(f"segment {seg['idx']}: {error}")
    with conn:
        if last_ok and not conn.execute("SELECT 1 FROM records WHERE document_id=? AND kind='source'",
                                        (document_id,)).fetchone():
            _insert_record(conn, last_ok[0], document_id, last_ok[1], "source", RecordContent.empty("source"))
        refresh_document(conn, document_id)
    return summary
