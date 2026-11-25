"""Review service: the only layer that changes review state. The web API (and a
future MCP server) are thin wrappers around these functions.

Records have three kinds: source -> condition -> observation. Links between them
are AI-written labels inside the record content (condition_key / condition_ref ...);
refresh_document resolves them into records.parent_id and recomputes every flag
of the document. System IDs (study_id, condition_id, ...) are derived on read.
"""
import json
import re
import sqlite3

from .checks import check_record, flag, is_graph_value, record_key
from .recall import coverage, observation_quotes
from .db import now
from .fields import (FIELDS_BY_NAME, LEVEL_LABELS, FieldValue, RecordContent, ai_fields, choice_ok,
                     is_electrical)
from .textmatch import locate, normalize, parse_number

OPEN_STATES = ("pending", "needs_resolution")  # states in which a record can be edited / decided


class ReviewError(Exception):
    pass


class NotFound(ReviewError):
    pass


class Conflict(ReviewError):
    pass


class Invalid(ReviewError):
    pass


def _required(value: str | None, what: str) -> str:
    if value is None or not value.strip():
        raise Invalid(f"{what}不能为空")
    return value.strip()


def _row(conn: sqlite3.Connection, record_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    if row is None:
        raise NotFound(f"记录 {record_id} 不存在")
    return row


def _log(conn, record_id, reviewer, action, reason=None, field=None, old=None, new=None):
    conn.execute(
        """INSERT INTO review_log (record_id, reviewer, action, field, old_value, new_value, reason, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (record_id, reviewer, action, field, old, new, reason, now()))


def _key(s: str | None) -> str:
    return normalize(s or "")


# ---------- links, IDs and document-wide flags ----------

class DocView:
    """All records of one document, with links resolved from the current content."""

    def __init__(self, conn: sqlite3.Connection, document_id: int):
        self.rows = conn.execute("SELECT * FROM records WHERE document_id=? ORDER BY id", (document_id,)).fetchall()
        self.content = {r["id"]: RecordContent.model_validate_json(r["current_json"]) for r in self.rows}
        self.by_kind = {k: [r for r in self.rows if r["kind"] == k] for k in LEVEL_LABELS}
        live = lambda k: [r for r in self.by_kind[k] if r["status"] != "rejected"]  # noqa: E731
        self.link_flags: dict[int, list[dict]] = {r["id"]: [] for r in self.rows}
        self.parent: dict[int, int | None] = {}

        sources = live("source")
        cond_by_key: dict[str, list[sqlite3.Row]] = {}
        for c in live("condition"):
            k = _key(self.content[c["id"]].get("condition_key"))
            if k:
                cond_by_key.setdefault(k, []).append(c)
        obs_by_key: dict[str, list[sqlite3.Row]] = {}
        for o in live("observation"):
            k = _key(self.content[o["id"]].get("observation_key"))
            if k:
                obs_by_key.setdefault(k, []).append(o)

        for r in self.by_kind["source"]:
            self.parent[r["id"]] = None
            proposed = RecordContent.model_validate_json(r["proposed_json"])
            if not any(fv.value for fv in proposed.fields.values()):
                self._add(r, "layer_missing", "error", "AI 没有返回论文层信息，这是系统建的空记录，请补填")
        for c in self.by_kind["condition"]:
            same_run = [s for s in sources if s["run_id"] == c["run_id"]]
            target = (same_run or sources or [None])[0]
            self.parent[c["id"]] = target["id"] if target else None
            if target is None:
                self._add(c, "layer_missing", "error", "这篇文档没有论文层记录，条件无法挂到论文上")
            key = _key(self.content[c["id"]].get("condition_key"))
            if key and len(cond_by_key.get(key, [])) > 1 and c["status"] != "rejected":
                self._add(c, "duplicate_key", "warning",
                          f"condition_key「{self.content[c['id']].get('condition_key')}」被多个条件使用",
                          "condition_key")
            self._check_ref(c, "control_condition_ref", cond_by_key)
        for o in self.by_kind["observation"]:
            ref = self.content[o["id"]].get("condition_ref")
            self.parent[o["id"]] = None
            if ref:
                cands = cond_by_key.get(_key(ref), [])
                if not cands:
                    self._add(o, "condition_ref_not_found", "error",
                              f"condition_ref「{ref}」找不到对应的条件（condition_key）", "condition_ref")
                else:
                    same_run = [c for c in cands if c["run_id"] == o["run_id"]]
                    self.parent[o["id"]] = (same_run or cands)[0]["id"]
                    if len(cands) > 1 and len(same_run) != 1:
                        self._add(o, "ambiguous_ref", "warning",
                                  f"condition_ref「{ref}」对应多个条件，已挂到第一个", "condition_ref")
            self._check_ref(o, "comparator_condition_ref", cond_by_key)
            self._check_ref(o, "comparator_observation_ref", obs_by_key)

        self.electrical: dict[int, bool] = {}
        for o in self.by_kind["observation"]:
            c = self.content[o["id"]]
            self.electrical[o["id"]] = is_electrical(c.get("trait_group"), c.get("metric_code"))
        for c in self.by_kind["condition"]:
            self.electrical[c["id"]] = any(self.electrical[o["id"]] for o in self.by_kind["observation"]
                                           if self.parent[o["id"]] == c["id"])
        for s in self.by_kind["source"]:
            self.electrical[s["id"]] = any(self.electrical[c["id"]] for c in self.by_kind["condition"]
                                           if self.parent[c["id"]] == s["id"])

    def _add(self, row, code, level, message, field=None):
        self.link_flags[row["id"]].append(flag(code, level, message, field))

    def _check_ref(self, row, field, targets):
        ref = self.content[row["id"]].get(field)
        if ref and _key(ref) not in targets:
            self._add(row, "ref_not_found", "warning", f"{field}「{ref}」找不到对应的记录", field)

    def resolve(self, row_id: int, field: str) -> int | None:
        """Record id that a link field (control/comparator ref) points to, if any."""
        ref = _key(self.content[row_id].get(field))
        if not ref:
            return None
        kind, key_field = ("observation", "observation_key") if field == "comparator_observation_ref" \
            else ("condition", "condition_key")
        for r in self.by_kind[kind]:
            if r["status"] != "rejected" and _key(self.content[r["id"]].get(key_field)) == ref:
                return r["id"]
        return None

    # ----- system IDs -----
    def ids(self, document_id: int) -> dict:
        srcs = self.by_kind["source"]
        primary = next((s for s in srcs if s["status"] != "rejected"), srcs[0] if srcs else None)
        study = _study_id(self.content[primary["id"]] if primary else None, document_id)
        out: dict[int, dict] = {}
        for n, s in enumerate(srcs, 1):
            out[s["id"]] = {"system_id": f"{study}-S{n}"}
        exps: dict[str, str] = {}
        arms: dict[str, str] = {}
        for n, c in enumerate(self.by_kind["condition"], 1):
            cc = self.content[c["id"]]
            e, a = _key(cc.get("experiment_ref")), _key(cc.get("arm_ref"))
            if e and e not in exps:
                exps[e] = f"{study}-E{len(exps) + 1}"
            if a and a not in arms:
                arms[a] = f"{study}-A{len(arms) + 1}"
            out[c["id"]] = {"system_id": f"{study}-C{n}", "experiment_id": exps.get(e), "arm_id": arms.get(a)}
        for n, o in enumerate(self.by_kind["observation"], 1):
            out[o["id"]] = {"system_id": f"{study}-O{n}"}
        return {"study_id": study, "records": out}


def _study_id(source: RecordContent | None, document_id: int) -> str:
    """Same convention as the Paper Distribution sheet: YYYY_SURNAME (e.g. 2023_ZHANG)."""
    if source:
        year = source.get("publication_year")
        author = source.get("first_author")
        if year and author:
            surname = author.split(",")[0].strip() if "," in author else author.split()[-1]
            surname = re.sub(r"[^A-Za-z\-]", "", surname).upper()
            y = re.search(r"\d{4}", year)
            if surname and y:
                return f"{y.group()}_{surname}"
    return f"DOC{document_id}"


def summary(kind: str, content: RecordContent) -> str:
    g = content.get
    if kind == "source":
        parts = [g("article_title") or g("doi"), g("paper_verdict")]
    elif kind == "condition":
        parts = [g("condition_key"), g("organism_name_reported"), g("group_type")]
    else:
        parts = [g("metric_code"), " ".join(x for x in (g("value_original") or g("value_numeric"),
                                                         g("unit_original")) if x), f"@{g('condition_ref') or '?'}"]
    return " · ".join(p for p in parts if p)


def _observation_key(doc: DocView, r: sqlite3.Row) -> tuple | None:
    """Same condition + metric_code + value + unit + time = the same result. None if not comparable."""
    c = doc.content[r["id"]]
    metric = _key(c.get("metric_code"))
    num = parse_number(c.get("value_numeric") or "")
    value = num if num is not None else _key(c.get("value_original"))
    if not metric or value in (None, ""):
        return None
    cond = doc.parent[r["id"]] or ("ref", _key(c.get("condition_ref")))
    t = parse_number(c.get("time_value") or "")
    time = (t, _key(c.get("time_unit"))) if t is not None else _key(c.get("measurement_time_original"))
    return cond, metric, value, _key(c.get("unit_original")), time


def _series_without_x(doc: DocView, live: list[sqlite3.Row]) -> dict[int, int]:
    """v9 #3: figure values with the same condition, metric, unit and time but different values are
    points of a series whose x value (light intensity, concentration, ...) was not recorded.
    Returns observation id -> number of distinct values in its group."""
    groups: dict[tuple, dict[int, object]] = {}
    for r in live:
        if r["kind"] != "observation" or not is_graph_value("observation", doc.content[r["id"]]):
            continue
        k = _observation_key(doc, r)
        if k:
            cond, metric, value, unit, time = k
            groups.setdefault((cond, metric, unit, time), {})[r["id"]] = value
    out: dict[int, int] = {}
    for members in groups.values():
        n = len(set(members.values()))
        if n > 1:
            out.update({i: n for i in members})
    return out


QUOTE_CODES = ("quote_missing", "quote_not_found", "value_not_in_quote")


def _once_per_template_field(doc: DocView, computed: dict[int, list[dict]], rid: dict) -> None:
    """v11 #5: a quote problem of a field copied from a condition template is one problem. Keep it on
    the first condition that has it (with a count of the others) and drop the copies. After a
    reviewer fixes it there, the field is that condition's own and the next copy is reported."""
    groups: dict[tuple, list[int]] = {}
    for r in doc.rows:
        if r["kind"] != "condition":
            continue
        c = doc.content[r["id"]]
        for f in computed[r["id"]]:
            t = c.from_template.get(f["field"] or "")
            if t and f["code"] in QUOTE_CODES:
                fv = c.fields.get(f["field"])
                key = (t, f["field"], f["code"], normalize(fv.value or "") if fv else "")
                groups.setdefault(key, []).append(r["id"])
    for (t, field, code, _), ids in groups.items():
        if len(ids) < 2:
            continue
        first, rest = ids[0], set(ids[1:])
        for f in computed[first]:
            if f["field"] == field and f["code"] == code:
                f["message"] += (f"（模板 {t} 的字段，另有 {len(rest)} 个条件同样如此，只在这里报一次：" +
                                 ", ".join(rid[i]["system_id"] for i in ids[1:6]) + ("…" if len(rest) > 5 else "") + "）")
        for i in rest:
            computed[i] = [f for f in computed[i] if not (f["field"] == field and f["code"] == code)]


def _suspected_fabricated_series(doc: DocView, live: list[sqlite3.Row]) -> dict[int, str]:
    """v11 #2: figure values whose x values (the setting that varies between their conditions) form a
    perfect arithmetic sequence AND that have three or more identical y values in a row -- the
    pattern of points filled in by assumption rather than read from the figure."""
    groups: dict[tuple, list[int]] = {}
    for r in live:
        c = doc.content[r["id"]]
        if r["kind"] == "observation" and is_graph_value("observation", c) and doc.parent[r["id"]]:
            key = (_key(c.get("source_locator")), _key(c.get("metric_code")), _key(c.get("unit_original")))
            groups.setdefault(key, []).append(r["id"])
    out: dict[int, str] = {}
    for ids in groups.values():
        if len(ids) < 4:
            continue
        conds = [doc.content[doc.parent[i]] for i in ids]
        ys = [parse_number(doc.content[i].get("value_numeric") or doc.content[i].get("value_original") or "") for i in ids]
        if any(y is None for y in ys):
            continue
        for spec in ai_fields("condition"):
            if spec.type not in ("decimal", "integer"):
                continue
            xs = [parse_number(c.get(spec.name) or "") for c in conds]
            if any(x is None for x in xs) or len(set(xs)) != len(xs):
                continue
            pts = sorted(zip(xs, ys))
            steps = [b[0] - a[0] for a, b in zip(pts, pts[1:])]
            even = all(abs(d - steps[0]) <= 1e-9 * max(1.0, abs(steps[0])) for d in steps)
            run = best = 1
            for a, b in zip(pts, pts[1:]):
                run = run + 1 if a[1] == b[1] else 1
                best = max(best, run)
            if even and best >= 3:
                msg = (f"疑似补出来的系列：{spec.name} 是等差数列（步长 {steps[0]:g}），且有 {best} 个连续相同的 y 值；"
                       "请对照原图核对点数和横坐标")
                out.update({i: msg for i in ids})
                break
    return out


def _missing_vs_sibling(doc: DocView, live: list[sqlite3.Row], rid: dict) -> dict[int, list[dict]]:
    """Warn when another condition of the same experiment has a field that this one leaves empty."""
    by_exp: dict[str, list[sqlite3.Row]] = {}
    for r in live:
        if r["kind"] == "condition":
            e = _key(doc.content[r["id"]].get("experiment_ref"))
            if e:
                by_exp.setdefault(e, []).append(r)
    out: dict[int, list[dict]] = {}
    names = [f.name for f in ai_fields("condition") if f.filled_by == "ai"]
    for group in by_exp.values():
        for r in group:
            for name in names:
                if doc.content[r["id"]].get(name):
                    continue
                having = [rid[s["id"]]["system_id"] for s in group
                          if s["id"] != r["id"] and doc.content[s["id"]].get(name)]
                if having:
                    out.setdefault(r["id"], []).append(flag(
                        "missing_vs_sibling", "warning",
                        f"同一实验的 {', '.join(having)} 填了 {name}，这条没填，请确认是否漏提取", name))
    return out


def refresh_document(conn: sqlite3.Connection, document_id: int) -> None:
    """Re-resolve links and recompute all flags of a document. Caller owns the transaction."""
    text = conn.execute("SELECT text FROM documents WHERE id=?", (document_id,)).fetchone()["text"]
    doc = DocView(conn, document_id)
    live = [r for r in doc.rows if r["status"] != "rejected"]

    # Sources / conditions: exact duplicates. Observations: same result (see _observation_key).
    exact: dict[tuple, list[int]] = {}
    same_result: dict[tuple, list[int]] = {}
    for r in live:
        if r["kind"] == "observation":
            k = _observation_key(doc, r)
            if k:
                same_result.setdefault(k, []).append(r["id"])
        else:
            # v9 #7: two conditions that differ only in their condition_key are the same condition
            ignore = ("condition_key",) if r["kind"] == "condition" else ()
            exact.setdefault((r["kind"], record_key(doc.content[r["id"]], ignore)), []).append(r["id"])
    dup_ids = {i for ids in exact.values() if len(ids) > 1 for i in ids}
    series = _series_without_x(doc, live)
    # v10 #3: result sentences no observation quotes, reported on the paper record
    _, uncovered = coverage(text, observation_quotes(doc.content[r["id"]] for r in live if r["kind"] == "observation"))
    recall_flags = [flag("uncovered_result", "warning",
                         f"结果句没有被任何观测值引用（可能漏提）：{text[a:b].strip()[:300]}") for a, b in uncovered]
    dup_obs = {i: ids for ids in same_result.values() if len(ids) > 1 for i in ids}
    rid = doc.ids(document_id)["records"]
    sibling_flags = _missing_vs_sibling(doc, live, rid)
    fabricated = _suspected_fabricated_series(doc, live)
    linked = {doc.parent[r["id"]] for r in live if r["kind"] == "observation"}

    computed: dict[int, list[dict]] = {}
    for r in doc.rows:
        flags = check_record(r["kind"], doc.content[r["id"]], text, doc.electrical[r["id"]])
        flags += doc.link_flags[r["id"]]
        if r["id"] in dup_ids:
            flags.append(flag("duplicate", "warning", "同一文档中有内容完全相同的另一条同层记录"))
        if r["id"] in dup_obs:
            others = ", ".join(rid[i]["system_id"] for i in dup_obs[r["id"]] if i != r["id"])
            flags.append(flag("duplicate_observation", "warning",
                              f"与 {others} 是同一结果（同条件、metric_code、数值、单位、时间）"))
        flags += sibling_flags.get(r["id"], [])
        if r["kind"] == "source":
            flags += recall_flags
        if r["id"] in series:
            flags.append(flag("series_without_x", "warning",
                              f"同一条件下有 {series[r['id']]} 个从图上读的不同值（同一指标、同一时间），"
                              "看不出各对应哪个自变量值；系列里每个点应各有一个条件", "condition_ref"))
        if r["id"] in fabricated:
            flags.append(flag("suspected_fabricated_series", "warning", fabricated[r["id"]], "value_original"))
        if r["kind"] == "condition" and r["status"] != "rejected" and r["id"] not in linked:
            flags.append(flag("condition_without_observation", "warning",
                              "没有任何观测值挂在这个条件下（条件只应为有结果的情况建立）"))
        computed[r["id"]] = flags

    _once_per_template_field(doc, computed, rid)
    for r in doc.rows:
        flags = computed[r["id"]]
        new = json.dumps(flags, ensure_ascii=False)
        parent = doc.parent[r["id"]]
        if new != r["flags_json"] or parent != r["parent_id"]:
            conn.execute("UPDATE records SET flags_json=?, parent_id=? WHERE id=?", (new, parent, r["id"]))


# ---------- queries ----------

def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT d.id, d.title, d.created_at,
                  COUNT(r.id) AS total,
                  SUM(r.status='pending') AS pending,
                  SUM(r.status='approved') AS approved,
                  SUM(r.status='rejected') AS rejected,
                  SUM(r.status='needs_resolution') AS needs_resolution
           FROM documents d LEFT JOIN records r ON r.document_id = d.id
           GROUP BY d.id ORDER BY d.id""").fetchall()
    counts = ("total", "pending", "approved", "rejected", "needs_resolution")
    return [{k: (r[k] or 0) if k in counts else r[k] for k in r.keys()} for r in rows]


def get_document(conn: sqlite3.Connection, document_id: int) -> dict:
    r = conn.execute("SELECT id, title, source_path, text FROM documents WHERE id=?", (document_id,)).fetchone()
    if r is None:
        raise NotFound(f"文档 {document_id} 不存在")
    return dict(r)


def list_records(conn: sqlite3.Connection, status: str | None = None, document_id: int | None = None,
                 flagged: bool | None = None, kind: str | None = None) -> list[dict]:
    sql = """SELECT r.id, r.document_id, d.title, r.kind, r.status, r.current_json, r.flags_json, r.reviewer
             FROM records r JOIN documents d ON d.id = r.document_id WHERE 1=1"""
    args: list = []
    for col, val in (("r.status", status), ("r.document_id", document_id), ("r.kind", kind)):
        if val is not None and val != "":
            sql += f" AND {col}=?"
            args.append(val)
    ids_cache: dict[int, dict] = {}
    out = []
    for r in conn.execute(sql + " ORDER BY r.document_id, r.id", args):
        flags = json.loads(r["flags_json"])
        if flagged is not None and bool(flags) != flagged:
            continue
        if r["document_id"] not in ids_cache:
            ids_cache[r["document_id"]] = DocView(conn, r["document_id"]).ids(r["document_id"])
        content = RecordContent.model_validate_json(r["current_json"])
        out.append({
            "id": r["id"], "document_id": r["document_id"], "document_title": r["title"],
            "kind": r["kind"], "system_id": ids_cache[r["document_id"]]["records"][r["id"]]["system_id"],
            "status": r["status"], "reviewer": r["reviewer"],
            "errors": sum(f["level"] == "error" for f in flags),
            "warnings": sum(f["level"] == "warning" for f in flags),
            "summary": summary(r["kind"], content),
        })
    return out


def _brief(r: sqlite3.Row, doc: DocView, ids: dict) -> dict:
    return {"id": r["id"], "kind": r["kind"], "status": r["status"],
            "system_id": ids["records"][r["id"]]["system_id"], "summary": summary(r["kind"], doc.content[r["id"]])}


def get_record(conn: sqlite3.Connection, record_id: int) -> dict:
    r = _row(conn, record_id)
    d = conn.execute("SELECT title, text FROM documents WHERE id=?", (r["document_id"],)).fetchone()
    seg = conn.execute('SELECT idx, start, "end" FROM segments WHERE id=?', (r["segment_id"],)).fetchone()
    run = conn.execute("SELECT * FROM extraction_runs WHERE id=?", (r["run_id"],)).fetchone()
    doc = DocView(conn, r["document_id"])
    ids = doc.ids(r["document_id"])
    rows_by_id = {x["id"]: x for x in doc.rows}
    current = RecordContent.model_validate_json(r["current_json"])
    proposed = RecordContent.model_validate_json(r["proposed_json"])

    fields = []
    for spec in ai_fields(r["kind"]):
        cur = current.fields.get(spec.name) or proposed.fields.get(spec.name)
        pro = proposed.fields.get(spec.name)
        cur_v, cur_q = (cur.value, cur.quote) if cur else (None, None)
        pro_v, pro_q = (pro.value, pro.quote) if pro else (None, None)
        hl = locate(d["text"], cur_q, prefer_from=seg["start"])
        fields.append({
            "name": spec.name, "label": spec.label, "group": spec.group, "type": spec.type,
            "filled_by": spec.filled_by, "required": spec.essential, "choices": spec.choices,
            "electrical_only": spec.electrical_only,
            "value": cur_v, "quote": cur_q, "proposed_value": pro_v, "proposed_quote": pro_q,
            "edited": (cur_v, cur_q) != (pro_v, pro_q),
            "highlight": list(hl) if hl else None,
        })
    parent = rows_by_id.get(r["parent_id"]) if r["parent_id"] else None
    chain = []
    while parent is not None:
        chain.append(_brief(parent, doc, ids))
        parent = rows_by_id.get(parent["parent_id"]) if parent["parent_id"] else None
    children = [_brief(x, doc, ids) for x in doc.rows if x["parent_id"] == r["id"]]
    log = [dict(x) for x in conn.execute("SELECT * FROM review_log WHERE record_id=? ORDER BY id", (record_id,))]
    info = ids["records"][r["id"]]
    return {
        "id": r["id"], "kind": r["kind"], "document_id": r["document_id"], "document_title": d["title"],
        "study_id": ids["study_id"], "system_id": info["system_id"],
        "experiment_id": info.get("experiment_id"), "arm_id": info.get("arm_id"),
        "is_electrical": doc.electrical[r["id"]],
        "parents": chain, "children": children,
        "segment": {"idx": seg["idx"], "start": seg["start"], "end": seg["end"],
                    "count": conn.execute("SELECT COUNT(*) FROM segments WHERE document_id=?",
                                          (r["document_id"],)).fetchone()[0]},
        "status": r["status"], "reviewer": r["reviewer"], "reviewed_at": r["reviewed_at"],
        "flags": json.loads(r["flags_json"]),
        "fields": fields,
        "run": {"id": run["id"], "model": run["model"], "base_url": run["base_url"],
                "prompt_version": run["prompt_version"], "schema_version": run["schema_version"],
                "created_at": run["created_at"], "raw_response": run["raw_response"], "error": run["error"]},
        "log": log,
    }


# ---------- actions ----------

def _decide(conn, record_id, reviewer, new_status, action, reason, allowed_from, verb):
    with conn:
        r = _row(conn, record_id)
        if r["status"] not in allowed_from:
            raise Conflict(f"记录状态是 {r['status']}，不能{verb}")
        reviewed_at = None if new_status == "pending" else now()
        conn.execute("UPDATE records SET status=?, reviewer=?, reviewed_at=? WHERE id=?",
                     (new_status, reviewer, reviewed_at, record_id))
        _log(conn, record_id, reviewer, action, reason=reason)
        refresh_document(conn, r["document_id"])
    return get_record(conn, record_id)


def approve(conn: sqlite3.Connection, record_id: int, reviewer: str, note: str | None = None) -> dict:
    reviewer = _required(reviewer, "审核人")
    return _decide(conn, record_id, reviewer, "approved", "approve", (note or "").strip() or None,
                   OPEN_STATES, "确认")


def reject(conn: sqlite3.Connection, record_id: int, reviewer: str, reason: str) -> dict:
    reviewer = _required(reviewer, "审核人")
    reason = _required(reason, "拒绝原因")
    return _decide(conn, record_id, reviewer, "rejected", "reject", reason, OPEN_STATES, "拒绝")


def flag_for_resolution(conn: sqlite3.Connection, record_id: int, reviewer: str, reason: str) -> dict:
    """pending -> needs_resolution: the reviewer cannot decide alone (review_status = needs_resolution)."""
    reviewer = _required(reviewer, "审核人")
    reason = _required(reason, "待解决原因")
    return _decide(conn, record_id, reviewer, "needs_resolution", "flag", reason, ("pending",), "标记待解决")


def reopen(conn: sqlite3.Connection, record_id: int, reviewer: str, reason: str) -> dict:
    reviewer = _required(reviewer, "审核人")
    reason = _required(reason, "重新打开原因")
    return _decide(conn, record_id, reviewer, "pending", "reopen", reason,
                   ("approved", "rejected", "needs_resolution"), "重新打开")


def edit_field(conn: sqlite3.Connection, record_id: int, reviewer: str, field: str, reason: str,
               value: str | None = None, quote: str | None = None) -> dict:
    """Edit one field. `None` = leave unchanged, "" = clear."""
    reviewer = _required(reviewer, "审核人")
    reason = _required(reason, "修改原因")
    spec = FIELDS_BY_NAME.get(field)
    with conn:
        r = _row(conn, record_id)
        if spec is None or spec not in ai_fields(r["kind"]):
            raise Invalid(f"{LEVEL_LABELS[r['kind']]}记录没有可修改的字段：{field}")
        if r["status"] not in OPEN_STATES:
            raise Conflict(f"记录状态是 {r['status']}，请先重新打开再修改")
        if value and value.strip() and spec.type == "choice" and not choice_ok(spec, value):
            raise Invalid(f"{field} 只能填这些值：{', '.join(spec.choices or [])}, other, unspecified")
        content = RecordContent.model_validate_json(r["current_json"])
        content.from_template.pop(field, None)  # v11: edited -> the condition's own field
        fv = content.fields.setdefault(field, FieldValue())
        changes = []
        for attr, new in (("value", value), ("quote", quote)):
            if new is None:
                continue
            new = new.strip() or None
            old = getattr(fv, attr)
            if new != old:
                setattr(fv, attr, new)
                changes.append((field if attr == "value" else f"{field}.quote", old, new))
        if not changes:
            raise Invalid("没有任何改动")
        conn.execute("UPDATE records SET current_json=? WHERE id=?", (content.model_dump_json(), record_id))
        for fname, old, new in changes:
            _log(conn, record_id, reviewer, "edit", reason=reason, field=fname, old=old, new=new)
        refresh_document(conn, r["document_id"])
    return get_record(conn, record_id)


def reviewer_notes(conn: sqlite3.Connection, record_id: int) -> str | None:
    """Export column reviewer_notes: every reason / note in the record's review log, oldest first."""
    rows = conn.execute("SELECT action, field, reason FROM review_log WHERE record_id=? AND reason IS NOT NULL "
                        "ORDER BY id", (record_id,)).fetchall()
    notes, seen = [], set()
    for x in rows:
        line = f"[{x['action']}{' ' + x['field'] if x['field'] else ''}] {x['reason']}"
        if (x["action"], x["reason"]) in seen and x["action"] == "edit":
            continue  # one edit with value + quote logs the same reason twice
        seen.add((x["action"], x["reason"]))
        notes.append(line)
    return "; ".join(notes) or None
