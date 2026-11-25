"""Automatic checker: attaches flags to one record. Flags are hints; they never block approval.

Cross-record flags (links between levels, duplicates) are added by
review_service.refresh_document, which calls check_record for every record.
"""
import re

from .fields import QUOTE_FROM, RecordContent, ai_fields, choice_ok, suggested_ok
from .textmatch import locate, normalize, numbers_in, numbers_in_value, parse_number


def flag(code: str, level: str, message: str, field: str | None = None) -> dict:
    return {"code": code, "level": level, "field": field, "message": message}


# Value fields of an observation. When the value was read from a figure (PDF attached), its quote is
# the caption or a sentence pointing at the figure, so the number is not expected in the quote.
VALUE_FIELDS = {"value_original", "value_numeric", "value_min", "value_max", "uncertainty_value",
                "uncertainty_lower", "uncertainty_upper"}
GRAPH_METHODS = {"graph_estimated", "graph_only"}


def is_graph_value(kind: str, content: RecordContent) -> bool:
    return kind == "observation" and normalize(content.get("extraction_method") or "") in GRAPH_METHODS


def check_record(kind: str, content: RecordContent, doc_text: str, electrical: bool = False) -> list[dict]:
    """`electrical`: whether this record belongs to electrical data. Essential fields in the
    Electrical setup group are only required for electrical records."""
    flags: list[dict] = []
    graph = is_graph_value(kind, content)
    for spec in ai_fields(kind):
        fv = content.fields.get(spec.name)
        value = (fv.value or "").strip() if fv else ""
        quote = (fv.quote or "").strip() if fv else ""

        if not value:
            if spec.essential and (electrical or not spec.electrical_only):
                if kind == "condition" and spec.name == "organism_name_reported":  # v9 #1
                    flags.append(flag("no_organism", "error", "条件没有物种名：多半是没有引用写了物种的模板"
                                      "（条件可以同时引用多个模板）", spec.name))
                elif spec.name in content.not_stated:  # the AI looked: the paper does not say
                    flags.append(flag("not_stated", "warning",
                                      f"必填字段 {spec.name}：AI 判断原文未写，导出为 {spec.missing or '空'}", spec.name))
                else:
                    flags.append(flag("missing_required", "error", f"必填字段 {spec.name} 为空", spec.name))
            continue
        if spec.filled_by == "link":
            continue

        if spec.type == "decimal" and parse_number(value) is None:
            flags.append(flag("not_numeric", "error", f"{spec.name} 不是数字：{value}", spec.name))
        elif spec.type == "integer":
            n = parse_number(value)
            if n is None or n != int(n):
                flags.append(flag("not_integer", "error", f"{spec.name} 不是整数：{value}", spec.name))
        elif spec.type == "choice" and not choice_ok(spec, value):
            flags.append(flag("choice_not_allowed", "error",
                              f"{spec.name} 的值「{value}」不在临时选项里（可用 other / unspecified）", spec.name))
        elif not suggested_ok(spec, value):
            flags.append(flag("not_in_temp_list", "warning",
                              f"{spec.name} 的值「{value}」不在临时列表里（待 Carly 确认列表）", spec.name))

        if spec.name == "evidence_excerpt" and locate(doc_text, value) is None:
            flags.append(flag("quote_not_found", "warning", "evidence_excerpt 在原文中找不到", spec.name))

        borrowed = not quote and spec.name in QUOTE_FROM  # its source field's quote is checked there
        if borrowed:
            src = content.fields.get(QUOTE_FROM[spec.name])
            quote = (src.quote or "").strip() if src else ""
        if not quote:
            if spec.needs_quote:
                flags.append(flag("quote_missing", "error", f"{spec.name} 没有原文引文", spec.name))
            continue
        if not borrowed and locate(doc_text, quote) is None:
            flags.append(flag("quote_not_found", "error", f"{spec.name} 的引文在原文中找不到", spec.name))
        if graph and spec.name in VALUE_FIELDS:
            continue
        if spec.needs_quote and spec.name != "value_original" and not _value_in_quote(value, quote, spec.type, spec.verbatim):
            flags.append(flag("value_not_in_quote", "warning", f"{spec.name} 的值没有出现在引文中", spec.name))

    if kind == "observation":
        flags += _value_numeric_consistency(content, graph)
    if graph:
        loc = content.get("source_locator") or ""
        if content.get("value_original") or content.get("value_numeric"):
            flags.append(flag("graph_value", "warning",
                              f"数值是从图中读出的（{content.get('extraction_method')}），请对照 "
                              f"{loc or '图'} 核对", "value_original"))
        if not re.search(r"fig", loc, re.I):
            flags.append(flag("graph_locator_missing", "error",
                              "图中读取的值必须在 source_locator 写明图号（如 Figure 3a）", "source_locator"))
    if kind == "condition" and normalize(content.get("potential_applied") or "") == "yes" \
            and not content.get("applied_potential_value"):
        flags.append(flag("applied_potential_missing", "warning",
                          "potential_applied 是 yes，但没有 applied_potential_value", "applied_potential_value"))
    if kind == "source":
        verdict = normalize(content.get("paper_verdict") or "")
        for v, need in (("exclude", "exclusion_reason"), ("uncertain", "uncertain_reason")):
            if verdict == v and not content.get(need):
                flags.append(flag("missing_required", "error",
                                  f"paper_verdict 是 {v}，必须填写 {need}", need))
    return flags


def _value_numeric_consistency(content: RecordContent, graph: bool = False) -> list[dict]:
    """value_numeric must be one of the numbers in value_original, and value_original must be
    word-for-word in its own quote. Both are errors: the numeric column is what gets analysed."""
    out = []
    num_s, orig = content.get("value_numeric"), content.get("value_original")
    orig_fv = content.fields.get("value_original")
    if num_s and orig:
        n = parse_number(num_s)
        if n is not None and not _num_in(n, numbers_in_value(orig)):
            out.append(flag("value_numeric_mismatch", "error",
                            f"value_numeric {num_s} 不在 value_original「{orig}」里", "value_numeric"))
    if not graph and orig and orig_fv and orig_fv.quote and normalize(orig) not in normalize(orig_fv.quote):
        out.append(flag("value_numeric_mismatch", "error",
                        f"value_original「{orig}」没有原样出现在它的引文里", "value_original"))
    return out


def _num_in(n: float, nums: list[float]) -> bool:
    return any(abs(n - q) <= 1e-9 * max(1.0, abs(n)) for q in nums)


def _value_in_quote(value: str, quote: str, ftype: str, verbatim: bool) -> bool:
    if ftype in ("decimal", "integer"):
        n = parse_number(value)
        return n is not None and _num_in(n, numbers_in(quote))
    if ftype == "choice":
        return True  # normalized category, not expected to be word-for-word
    if verbatim:
        return normalize(value) in normalize(quote)
    quote_nums = numbers_in(quote)
    return all(_num_in(n, quote_nums) for n in numbers_in(value))


def record_key(content: RecordContent, ignore: tuple = ()) -> tuple:
    return tuple(sorted((n, normalize(fv.value or "")) for n, fv in content.fields.items()
                        if n not in ignore and (fv.value or "").strip()))
