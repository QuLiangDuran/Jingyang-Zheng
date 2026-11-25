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

from .db import now
from .documents import segment_text
from .fields import FIELDS_BY_NAME, SCHEMA_VERSION, TRAIT_TO_METRIC, FieldValue, RecordContent, ai_fields
from .llm import LLMClient
from .review_service import refresh_document

PROMPT_VERSION = "v4"

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
    if f.suggested:
        bits.append("use one of: " + " | ".join(f.suggested + ["other"]))
    if f.filled_by == "link":
        bits.append("plain string label")
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
    item: dict = {}
    for f in _example_fields(level):
        if f.filled_by == "link":
            item[f.name] = "..."
        else:
            item[f.name] = {"value": "...", "quote": "..." if f.needs_quote else None}
    return item


def response_example() -> dict:
    """The response shape shown to the AI, generated from fields.py."""
    return {"source": _example_item("source"),
            "conditions": [_example_item("condition")],
            "observations": [_example_item("observation")]}


def empty_response() -> dict:
    return {"source": None, "conditions": [], "observations": []}


def _options(name: str) -> str:
    return " | ".join(FIELDS_BY_NAME[name].choices)


def build_system_prompt() -> str:
    """Rules merged from docs/extraction_rules_prompt.txt (field names mapped to fields.py,
    decisions of 2026-11-25); every option list and field name comes from fields.py."""
    field_lines = "\n\n".join(
        f'"{LAYER_KEYS[lv]}" -- {LEVEL_NAMES[lv]}:\n' + "\n".join(_field_line(f) for f in ai_fields(lv))
        for lv in LAYER_KEYS)
    trait_map = ", ".join(f"{t} -> {' / '.join(ms)}" for t, ms in TRAIT_TO_METRIC.items())
    return f"""You are extracting records for the MicroalgaeMatch database from the text of ONE scientific paper.
Current focus: bio-photovoltaics (BPV).

Fields of each record:
{field_lines}

=== RECORD STRUCTURE ===
- Return three levels: "source" (the paper), "conditions" and "observations".
- One observation = one measured trait (one quantity at one time). It links to exactly one
  condition: its "condition_ref" is that condition's "condition_key".
- One condition = one organism + one set of conditions (cultivation and, for electrical
  results, the device setup).
- Do not extract a condition on its own: only conditions that at least one observation links to.
- If you can tell which organism produced a result but not all of its conditions, still extract
  it: create a condition with the organism and whatever is stated, and link the observation to it.
  Only skip a result if you cannot tell which organism AND which condition produced it.

=== VALUES, QUOTES, MISSING DATA ===
- Extract only what the paper states. Never infer or guess a value that is not written, and do
  not convert units.
- Allowed format normalization: parse value_numeric from value_original; map the paper's wording
  to one of the listed options; split a light:dark cycle such as 16:8 into light_hours and dark_hours.
- For every field you fill, "quote" MUST be copied verbatim from the text (a short span, ideally
  one sentence or less) that shows the value. Fields marked "no quote needed" are judgements and
  take no quote.
- If a field is not stated, omit it. Missing details do NOT remove a record; missing required
  fields are flagged by the system.

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
- trait_group and metric_code: use the values listed in the field descriptions above.
  Traits of interest and their metric_code: {trait_map}.
- A clearly described non-numeric observation counts too: put it in value_category and set
  value_status = observation.
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

=== PAPER VERDICT ===
paper_verdict: {_options("paper_verdict")}.
- include: at least one eligible observation AND (for the BPV focus) at least one electrical one.
- exclude: give exclusion_reason as one of {_options("exclusion_reason")}.
- uncertain: the needed information is probably in figures, supplements or another document you
  were not given. Say what is missing in uncertain_reason.

=== OUTPUT ===
- If there are no records, return {json.dumps(empty_response())}.
- Respond with JSON only, no commentary, in this shape (only a few fields are shown; use the
  field names listed above as keys):
{json.dumps(response_example(), ensure_ascii=False)}"""


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


def _insert_record(conn, run_id, document_id, segment_id, kind, content: RecordContent) -> int:
    js = content.model_dump_json()
    return conn.execute(
        """INSERT INTO records (run_id, document_id, segment_id, kind, proposed_json, current_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (run_id, document_id, segment_id, kind, js, js, now())).lastrowid


def extract_document(conn: sqlite3.Connection, document_id: int, llm: LLMClient) -> ExtractSummary:
    doc = conn.execute("SELECT text FROM documents WHERE id=?", (document_id,)).fetchone()
    if doc is None:
        raise ValueError(f"document {document_id} not found")
    text = doc["text"]
    system = build_system_prompt()
    summary = ExtractSummary(document_id)
    last_ok: tuple[int, int] | None = None  # (run_id, segment_id)

    segments = conn.execute('SELECT id, idx, start, "end" FROM segments WHERE document_id=? ORDER BY idx',
                            (document_id,)).fetchall()
    for seg in segments:
        chunk = (seg["idx"], len(segments))
        user = build_user_prompt(text[seg["start"]:seg["end"]], chunk,
                                 _known_conditions(conn, document_id) if seg["idx"] > 0 else None)
        prompt = f"[system]\n{system}\n\n[user]\n{user}"
        raw, status, error, parsed = None, "ok", None, ParsedResponse()
        try:
            if hasattr(llm, "last_finish_reason"):
                llm.last_finish_reason = None
            raw = llm.complete(system, user)
            if getattr(llm, "last_finish_reason", None) == "length":
                # Not continued automatically: the reviewer / operator decides what to do.
                raise ParseError("输出被截断（finish_reason=length，超过 LLM_MAX_OUTPUT_TOKENS 或模型输出上限）；"
                                 "原始回答已保存，未自动续写")
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
        with conn:
            run_id = conn.execute(
                """INSERT INTO extraction_runs (document_id, segment_id, model, base_url, prompt_version,
                   schema_version, temperature, prompt, raw_response, status, error, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (document_id, seg["id"], llm.model, llm.base_url, PROMPT_VERSION, SCHEMA_VERSION,
                 llm.temperature, prompt, raw, status, error, now())).lastrowid
            for kind, content in records:
                _insert_record(conn, run_id, document_id, seg["id"], kind, content)
        if status == "ok":
            last_ok = (run_id, seg["id"])
        summary.runs += 1
        summary.records += len(records)
        summary.failed_runs += status != "ok"

    with conn:
        has_source = conn.execute("SELECT 1 FROM records WHERE document_id=? AND kind='source'",
                                  (document_id,)).fetchone()
        if not has_source and last_ok:
            # The AI returned no paper-level record. Create an empty one so conditions have a
            # parent and the reviewer has somewhere to fill paper fields; it is flagged.
            _insert_record(conn, last_ok[0], document_id, last_ok[1], "source", RecordContent.empty("source"))
        refresh_document(conn, document_id)
    return summary
